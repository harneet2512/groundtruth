"""Revision-bound LSP enrichment of a private graph candidate.

The scheduler returns a task handle and terminal receipt. The graph coordinator
owns publication after checking source and graph identities. Queried certified
graphs remain immutable. Legacy process-global entry points remain available
for compatibility callers.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import shutil
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LANGUAGE_SERVERS: dict[str, str] = {
    "python": "pyright-langserver",
    "typescript": "typescript-language-server",
    "javascript": "typescript-language-server",
    "go": "gopls",
    "rust": "rust-analyzer",
    "java": "jdtls",
}

_promotion_task: asyncio.Task[None] | None = None
_stats: dict[str, Any] = {"status": "idle"}

BATCH_SIZE = 50

PROMOTION_RECEIPT_SCHEMA = "gt.lsp_promotion_task.v1"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repository_identity(path: str | Path) -> str:
    material = str(Path(path).resolve()).encode("utf-8", "surrogatepass")
    return hashlib.sha256(material).hexdigest()


def _delete_sqlite_candidate(path: Path) -> list[str]:
    errors = []
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{candidate.name}:{type(exc).__name__}")
    return errors


def repository_snapshot_sha256(path: str | Path) -> str:
    """Hash an immutable source snapshot by relative path and file content."""
    root = Path(path).resolve()
    if not root.is_dir():
        raise ValueError("lsp_promotion_repository_snapshot_missing")
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*"), key=lambda candidate: candidate.as_posix()):
        if not item.is_file():
            continue
        relative = item.relative_to(root).as_posix().encode("utf-8", "surrogatepass")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = item.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class LSPPromotionRequest:
    """Immutable identity of one candidate-graph enrichment attempt."""

    source_revision: str
    graph_revision: str
    graph_path: str
    graph_sha256: str
    repository_root: str
    repository_snapshot_sha256: str
    candidate_path: str

    def __post_init__(self) -> None:
        if not self.source_revision or not self.graph_revision:
            raise ValueError("lsp_promotion_revision_required")
        for label, digest in (
            ("graph", self.graph_sha256),
            ("repository_snapshot", self.repository_snapshot_sha256),
        ):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"lsp_promotion_{label}_digest_invalid")
        source = Path(self.graph_path).resolve()
        candidate = Path(self.candidate_path).resolve()
        if source == candidate:
            raise ValueError("lsp_promotion_requires_candidate_copy")


class LSPPromotionTaskHandle:
    """Cancellation and terminal-receipt surface for one scheduled enrichment."""

    def __init__(self, task_id: str, request: LSPPromotionRequest) -> None:
        self.task_id = task_id
        self.request = request
        self._lock = threading.Lock()
        self._future: Future[dict[str, Any]] | None = None
        self._cancel_requested = False
        self._completion_sealed = False

    def _bind_future(self, future: Future[dict[str, Any]]) -> None:
        with self._lock:
            self._future = future
            cancel = self._cancel_requested
        if cancel:
            future.cancel()

    @property
    def done(self) -> bool:
        with self._lock:
            future = self._future
        return bool(future is not None and future.done())

    def cancel(self) -> bool:
        with self._lock:
            future = self._future
            if (
                self._cancel_requested
                or self._completion_sealed
                or (future is not None and future.done())
            ):
                return False
            self._cancel_requested = True
        if future is not None:
            future.cancel()
        return True

    @property
    def cancellation_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    def _seal_publishable_completion(self) -> bool:
        """Atomically prevent a later accepted cancellation from racing success."""
        with self._lock:
            if self._cancel_requested:
                return False
            self._completion_sealed = True
            return True

    def _cancelled_receipt(self) -> dict[str, Any]:
        request = self.request
        cleanup_errors = _delete_sqlite_candidate(Path(request.candidate_path))
        return {
            "schema": PROMOTION_RECEIPT_SCHEMA,
            "task_id": self.task_id,
            "terminal": True,
            "status": "cancelled",
            "reason": "cancel_requested_before_execution",
            "source_revision": request.source_revision,
            "input_graph_revision": request.graph_revision,
            "input_graph_sha256": request.graph_sha256,
            "repository_root_sha256": _repository_identity(request.repository_root),
            "repository_snapshot_sha256": request.repository_snapshot_sha256,
            "candidate_path": request.candidate_path,
            "output_graph_sha256": "",
            "languages_promotable": [],
            "languages_attempted": [],
            "languages_completed": [],
            "languages_unavailable": [],
            "all_promotable_languages_attempted": False,
            "language_fidelity": {},
            "publishable": False,
            "cancellation_mode": "safe_language_boundary",
            "cleanup_errors": cleanup_errors,
        }

    def terminal_receipt(self, *, timeout: float | None = None) -> dict[str, Any]:
        with self._lock:
            future = self._future
        if future is None:
            raise RuntimeError("lsp_promotion_not_scheduled")
        try:
            return dict(future.result(timeout=timeout))
        except CancelledError:
            return self._cancelled_receipt()


ServerDetector = Callable[[], Mapping[str, str]]
EdgeLoader = Callable[[str, str], list[dict[str, Any]]]
EdgeResolver = Callable[[str, str, list[dict[str, Any]], str], Awaitable[dict[str, Any]]]
ClosureRebuilder = Callable[[str], bool]


class LSPPromotionScheduler:
    """Produce revision-bound enriched candidates; never publish or mutate input.

    Publication remains the graph coordinator owner's job. A successful receipt
    only makes the candidate eligible for that owner-thread revision check.
    """

    def __init__(
        self,
        *,
        server_detector: ServerDetector | None = None,
        edge_loader: EdgeLoader | None = None,
        resolver: EdgeResolver | None = None,
        closure_rebuilder: ClosureRebuilder | None = None,
    ) -> None:
        self._server_detector = server_detector or detect_available_servers
        self._edge_loader = edge_loader or self._load_edges
        self._resolver = resolver or self._resolve_edges
        self._closure_rebuilder = closure_rebuilder or self._rebuild_closure
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gt-lsp")
        self._lock = threading.Lock()
        self._handles: list[LSPPromotionTaskHandle] = []
        self._closed = False

    @staticmethod
    def _load_edges(db_path: str, language: str) -> list[dict[str, Any]]:
        from groundtruth.resolve import _get_ambiguous_edges

        connection = sqlite3.connect(db_path)
        try:
            return _get_ambiguous_edges(connection, min_confidence=0.95, language=language)
        finally:
            connection.close()

    @staticmethod
    async def _resolve_edges(
        db_path: str, root_path: str, edges: list[dict[str, Any]], language: str
    ) -> dict[str, Any]:
        from groundtruth.resolve import _resolve_edges

        return await _resolve_edges(db_path, root_path, edges, language)

    @staticmethod
    def _rebuild_closure(db_path: str) -> bool:
        from groundtruth.resolve import _rebuild_closure

        return _rebuild_closure(db_path)

    def schedule(self, request: LSPPromotionRequest) -> LSPPromotionTaskHandle:
        with self._lock:
            if self._closed:
                raise RuntimeError("lsp_promotion_scheduler_closed")
            handle = LSPPromotionTaskHandle(secrets.token_hex(16), request)
            self._handles.append(handle)
            future = self._executor.submit(self._execute, handle)
            handle._bind_future(future)
            return handle

    @staticmethod
    def _copy_graph(source: Path, candidate: Path) -> None:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if candidate.exists():
            raise FileExistsError("lsp_promotion_candidate_exists")
        source_connection = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
        target_connection = sqlite3.connect(candidate)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()

    @staticmethod
    def _promotable_languages(path: Path) -> dict[str, int]:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(nodes)")}
            if {"node_type", "candidate_state"}.issubset(columns):
                rows = connection.execute(
                    "SELECT language,COUNT(*) FROM nodes "
                    "WHERE node_type='callsite' AND candidate_state='ambiguous' "
                    "GROUP BY language ORDER BY language"
                ).fetchall()
                return {str(language): int(count) for language, count in rows}
            rows = connection.execute(
                "SELECT src.language,COUNT(*) FROM edges e "
                "JOIN nodes src ON e.source_id=src.id "
                "WHERE e.resolution_method='name_match' AND e.type='CALLS' "
                "GROUP BY src.language ORDER BY src.language"
            ).fetchall()
        finally:
            connection.close()
        return {str(language): int(count) for language, count in rows}

    @staticmethod
    def _finalize_candidate(path: Path) -> str:
        """Checkpoint resolver WAL content into the single publishable DB file."""
        connection = sqlite3.connect(path, timeout=5.0)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise RuntimeError("lsp_promotion_checkpoint_busy")
            try:
                journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise RuntimeError("lsp_promotion_journal_transition_busy") from exc
                raise
            if journal_mode is None or str(journal_mode[0]).lower() != "delete":
                raise RuntimeError("lsp_promotion_journal_transition_incomplete")
            return str(connection.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            connection.close()

    def _base_receipt(self, handle: LSPPromotionTaskHandle) -> dict[str, Any]:
        request = handle.request
        return {
            "schema": PROMOTION_RECEIPT_SCHEMA,
            "task_id": handle.task_id,
            "terminal": True,
            "source_revision": request.source_revision,
            "input_graph_revision": request.graph_revision,
            "input_graph_sha256": request.graph_sha256,
            "repository_root_sha256": _repository_identity(request.repository_root),
            "repository_snapshot_sha256": request.repository_snapshot_sha256,
            "candidate_path": str(Path(request.candidate_path).resolve()),
            "output_graph_sha256": "",
            "languages_promotable": [],
            "languages_attempted": [],
            "languages_completed": [],
            "languages_unavailable": [],
            "all_promotable_languages_attempted": False,
            "language_fidelity": {},
            "language_receipts": {},
            "verified": 0,
            "corrected": 0,
            "deleted": 0,
            "failed": 0,
            "closure_rebuilt": False,
            "publishable": False,
            "cancellation_mode": "safe_language_boundary",
            "cleanup_errors": [],
        }

    async def _run_languages(
        self,
        handle: LSPPromotionTaskHandle,
        candidate: Path,
        languages: list[str],
        terminal: dict[str, Any],
    ) -> None:
        for language in languages:
            if handle.cancellation_requested:
                raise asyncio.CancelledError
            terminal["languages_attempted"].append(language)
            terminal["all_promotable_languages_attempted"] = (
                terminal["languages_attempted"] == terminal["languages_promotable"]
            )
            try:
                edges = self._edge_loader(str(candidate), language)
                result = await self._resolver(
                    str(candidate), handle.request.repository_root, edges, language
                )
            except (Exception, asyncio.CancelledError) as exc:
                terminal["language_receipts"][language] = {
                    "status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    "error_type": type(exc).__name__,
                }
                raise
            receipt = {
                key: value
                for key, value in result.items()
                if isinstance(value, (str, int, float, bool)) or value is None
            }
            receipt["loaded_edge_count"] = len(edges)
            receipt["selection_limit"] = 500
            receipt["candidate_unit_count"] = terminal["candidate_unit_counts"][language]
            receipt["selected_unit_count"] = len(
                {edge.get("callsite_stable_id") or edge.get("id") for edge in edges}
            )
            receipt["selection_complete"] = (
                receipt["selected_unit_count"] == receipt["candidate_unit_count"]
            )
            if not receipt["selection_complete"]:
                receipt["selection_limitation"] = "bounded_or_primary_identity_unavailable"
            terminal["language_receipts"][language] = receipt
            terminal["languages_completed"].append(language)
            for key in ("verified", "corrected", "deleted", "failed"):
                terminal[key] += int(result.get(key, 0) or 0)
            if handle.cancellation_requested:
                raise asyncio.CancelledError

    def _execute(self, handle: LSPPromotionTaskHandle) -> dict[str, Any]:
        receipt = self._base_receipt(handle)
        request = handle.request
        source = Path(request.graph_path).resolve()
        candidate = Path(request.candidate_path).resolve()
        try:
            if _file_sha256(source) != request.graph_sha256:
                raise ValueError("lsp_promotion_input_changed")
            wal_path = Path(f"{source}-wal")
            if wal_path.exists() and wal_path.stat().st_size:
                raise ValueError("lsp_promotion_input_has_unbound_wal")
            if repository_snapshot_sha256(request.repository_root) != (
                request.repository_snapshot_sha256
            ):
                raise ValueError("lsp_promotion_repository_snapshot_changed")
            self._copy_graph(source, candidate)
            if _file_sha256(source) != request.graph_sha256:
                raise ValueError("lsp_promotion_input_changed_during_copy")
            promotable = self._promotable_languages(candidate)
            receipt["languages_promotable"] = sorted(promotable)
            receipt["candidate_unit_counts"] = promotable
            servers = dict(sorted(self._server_detector().items()))
            attempted = [language for language in sorted(promotable) if language in servers]
            receipt["languages_unavailable"] = sorted(set(promotable) - set(attempted))
            receipt["language_fidelity"] = {
                language: {
                    "detected_executable": servers[language],
                    "scope": "definition_resolution_for_eligible_ambiguous_calls",
                }
                for language in attempted
            }
            if not promotable:
                receipt.update(status="no_op", reason="nothing_to_promote")
                receipt["cleanup_errors"] = _delete_sqlite_candidate(candidate)
                return receipt
            if not attempted:
                receipt.update(
                    status="unavailable",
                    reason="no_language_server_for_promotable_languages",
                )
                receipt["cleanup_errors"] = _delete_sqlite_candidate(candidate)
                return receipt
            asyncio.run(self._run_languages(handle, candidate, attempted, receipt))
            if handle.cancellation_requested:
                raise asyncio.CancelledError
            edge_mutations = sum(int(receipt[key]) for key in ("verified", "corrected", "deleted"))
            if edge_mutations:
                receipt["closure_rebuilt"] = bool(self._closure_rebuilder(str(candidate)))
                if not receipt["closure_rebuilt"]:
                    raise ValueError("lsp_promotion_closure_rebuild_failed")
            if handle.cancellation_requested:
                raise asyncio.CancelledError
            quick_check = self._finalize_candidate(candidate)
            if quick_check != "ok":
                raise ValueError("lsp_promotion_candidate_corrupt")
            if not handle._seal_publishable_completion():
                raise asyncio.CancelledError
            receipt.update(
                status="succeeded",
                reason=None,
                output_graph_sha256=_file_sha256(candidate),
                sqlite_quick_check=quick_check,
                publishable=True,
            )
            return receipt
        except asyncio.CancelledError:
            receipt["cleanup_errors"] = _delete_sqlite_candidate(candidate)
            receipt.update(status="cancelled", reason="cancel_requested")
            return receipt
        except Exception as exc:
            receipt["cleanup_errors"] = _delete_sqlite_candidate(candidate)
            receipt.update(status="failed", reason=f"{type(exc).__name__}:{str(exc)[:160]}")
            return receipt

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
            handles = list(self._handles)
        for handle in handles:
            if not handle.done:
                handle.cancel()
        self._executor.shutdown(wait=wait, cancel_futures=True)


def get_promotion_stats() -> dict[str, Any]:
    return dict(_stats)


def detect_available_servers() -> dict[str, str]:
    available = {}
    for lang, cmd in _LANGUAGE_SERVERS.items():
        resolved = shutil.which(cmd)
        if resolved:
            available[lang] = str(Path(resolved).resolve())
    return available


async def _promote_edges_progressive(
    db_path: str,
    root_path: str,
) -> None:
    """Promote name_match edges progressively, yielding between batches."""
    global _stats

    try:
        from groundtruth.resolve import _get_ambiguous_edges, _resolve_edges
    except ImportError:
        _stats = {"status": "skipped", "reason": "resolve module unavailable"}
        return

    available = detect_available_servers()
    if not available:
        _stats = {"status": "skipped", "reason": "no_lsp_servers_installed"}
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        nm_rows = conn.execute(
            """SELECT src.language, COUNT(*) as cnt
               FROM edges e JOIN nodes src ON e.source_id = src.id
               WHERE e.resolution_method = 'name_match' AND e.type = 'CALLS'
               GROUP BY src.language"""
        ).fetchall()
    except Exception:
        _stats = {"status": "skipped", "reason": "query_failed"}
        conn.close()
        return

    promotable = {row[0]: row[1] for row in nm_rows}
    conn.close()

    languages = [lang for lang in available if lang in promotable and promotable[lang] > 0]
    if not languages:
        _stats = {"status": "done", "reason": "nothing_to_promote", "promoted": 0}
        return

    _stats = {
        "status": "running",
        "total_promotable": sum(promotable.get(l, 0) for l in languages),
        "languages": languages,
        "verified": 0,
        "corrected": 0,
        "deleted": 0,
        "failed": 0,
    }

    for lang in languages:
        _stats["current_language"] = lang

        try:
            edge_conn = sqlite3.connect(db_path, timeout=30)
            edges = _get_ambiguous_edges(edge_conn, min_confidence=0.95, language=lang)
            edge_conn.close()
            lang_stats = await _resolve_edges(db_path, root_path, edges, lang)
        except Exception:
            _stats["failed"] += promotable.get(lang, 0)
            continue

        _stats["verified"] += lang_stats.get("verified", 0)
        _stats["corrected"] += lang_stats.get("corrected", 0)
        _stats["deleted"] += lang_stats.get("deleted", 0)
        _stats["failed"] += lang_stats.get("failed", 0)

        await asyncio.sleep(0)

    _stats["status"] = "done"
    _stats.pop("current_language", None)


def start_background_promotion(db_path: str, root_path: str) -> None:
    """Start progressive LSP promotion as background asyncio task.

    Safe to call from sync or async context. If no event loop is running
    yet, the task will be created when one starts.
    """
    global _promotion_task

    if _promotion_task is not None:
        return

    try:
        loop = asyncio.get_running_loop()
        _promotion_task = loop.create_task(
            _promote_edges_progressive(db_path, root_path),
            name="gt-lsp-promotion",
        )
    except RuntimeError:
        pass
