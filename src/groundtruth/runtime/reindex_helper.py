"""Layer-6 incremental re-indexing helper for the post-edit hook (v1.0.5).

==============================================================================
DEAD SURFACE — superseded by oh_gt_full_wrapper.make_reindex_command (the live
wrapper constructs the ``gt-index -file`` reindex command directly; L6 in the
DeepSWE path is gated OFF in gt_mini_patch). NOT routable on any live path (no
live entrypoint imports it; only a docstring comment references it). Retained
ONLY for the dead-path registry. A plain import still succeeds; CALLING the
producer (``check_and_reindex_modified_files``) raises by design.
See GT_ARCHITECTURE_LINEAGE.md.
==============================================================================

Runs at the top of the post-edit hook, before evidence queries. For each
modified file:

  1. mtime short-circuit — if graph.db mtime >= file mtime, mark "fresh"
     and skip subprocess.
  2. Otherwise — invoke ``gt-index --incremental --files=<file> --root=<root>
     --output=<db>`` with a hard budget. If gt-index doesn't support
     ``--incremental`` (current build), the call exits non-zero and we
     mark "stale_no_indexer".
  3. Record outcome via ``log_index_freshness`` (layer6 sink).

Exposes one entry point: ``check_and_reindex_modified_files``. Never
raises; on any failure the caller proceeds with whatever state graph.db
is in, and telemetry records the staleness.

Why this is honest, not lying:
  When graph.db is stale, downstream families (CALLER, STRUCTURAL, …)
  may still emit findings about *the pre-edit code shape*. Layer-6
  telemetry makes the staleness visible so a reviewer can spot when
  hook output reflects pre-edit state vs current state.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from typing import Any


_DEFAULT_BUDGET_S = 8.0
_GT_INDEX_BIN = os.environ.get("GT_INDEX_BIN", "/tmp/gt-index-linux")


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _hash_file(path: str, max_bytes: int = 1_000_000) -> str:
    """Cheap content hash for pre/post comparison; bounded read for safety."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes)
        return hashlib.sha256(data).hexdigest()[:12]
    except OSError:
        return ""


def _try_incremental_reindex(
    *,
    file_path: str,
    root: str,
    db_path: str,
    budget_s: float,
) -> tuple[str, float]:
    """Best-effort single-file reindex. Returns (outcome, elapsed_ms).

    Outcomes: ``fresh_after_reindex`` (db now >= file mtime),
    ``stale`` (reindex ran but db still behind),
    ``stale_no_indexer`` (binary missing or rejected --incremental flag),
    ``timeout``, ``error``.
    """
    if not os.path.exists(_GT_INDEX_BIN):
        return "stale_no_indexer", 0.0

    start = time.monotonic()
    try:
        result = subprocess.run(
            [
                _GT_INDEX_BIN,
                "--incremental",
                f"--files={file_path}",
                f"--root={root}",
                f"--output={db_path}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=budget_s,
            cwd=root,
        )
    except subprocess.TimeoutExpired:
        return "timeout", (time.monotonic() - start) * 1000.0
    except OSError:
        return "error", (time.monotonic() - start) * 1000.0

    elapsed_ms = (time.monotonic() - start) * 1000.0
    # Current gt-index build does not accept --incremental; flag.Parse()
    # rejects unknown flags with non-zero exit. Mark as stale_no_indexer
    # rather than running a full rebuild we didn't ask for.
    if result.returncode != 0:
        return "stale_no_indexer", elapsed_ms

    db_after = _mtime(db_path)
    file_mtime = _mtime(os.path.join(root, file_path))
    if db_after > 0 and db_after >= file_mtime:
        return "fresh_after_reindex", elapsed_ms
    return "stale", elapsed_ms


def check_and_reindex_modified_files(
    *,
    modified_files: list[str],
    root: str,
    db_path: str,
    instance_id: str | None = None,
    budget_s: float = _DEFAULT_BUDGET_S,
) -> dict[str, dict[str, Any]]:
    """Per-file freshness check + best-effort reindex. Emits layer6 telemetry.

    Returns a dict keyed by file path: ``{file: {"outcome": str,
    "elapsed_ms": float, "db_mtime_before": float, "db_mtime_after": float,
    "file_mtime": float}}``. Never raises.
    """
    raise RuntimeError(
        "DEAD SURFACE: groundtruth.runtime.reindex_helper."
        "check_and_reindex_modified_files superseded by "
        "oh_gt_full_wrapper.make_reindex_command; not routable. "
        "See GT_ARCHITECTURE_LINEAGE.md."
    )
    outcomes: dict[str, dict[str, Any]] = {}
    if not modified_files:
        return outcomes
    if not os.path.exists(db_path):
        # No index at all — every file is unindexed.
        for f in modified_files:
            outcomes[f] = {
                "outcome": "no_db",
                "elapsed_ms": 0.0,
                "db_mtime_before": 0.0,
                "db_mtime_after": 0.0,
                "file_mtime": _mtime(os.path.join(root, f)),
            }
            _emit(instance_id, f, outcomes[f])
        return outcomes

    for rel in modified_files:
        abs_path = rel if os.path.isabs(rel) else os.path.join(root, rel)
        db_before = _mtime(db_path)
        file_mt = _mtime(abs_path)
        pre_hash = _hash_file(abs_path)

        if db_before > 0 and file_mt > 0 and db_before >= file_mt:
            outcomes[rel] = {
                "outcome": "fresh",
                "elapsed_ms": 0.0,
                "db_mtime_before": db_before,
                "db_mtime_after": db_before,
                "file_mtime": file_mt,
                "pre_hash": pre_hash,
                "post_hash": pre_hash,
            }
            _emit(instance_id, rel, outcomes[rel])
            continue

        out, elapsed_ms = _try_incremental_reindex(
            file_path=rel, root=root, db_path=db_path, budget_s=budget_s
        )
        db_after = _mtime(db_path)
        post_hash = _hash_file(abs_path)
        outcomes[rel] = {
            "outcome": out,
            "elapsed_ms": elapsed_ms,
            "db_mtime_before": db_before,
            "db_mtime_after": db_after,
            "file_mtime": file_mt,
            "pre_hash": pre_hash,
            "post_hash": post_hash,
        }
        _emit(instance_id, rel, outcomes[rel])
    return outcomes


def _emit(instance_id: str | None, file_path: str, record: dict[str, Any]) -> None:
    """Best-effort layer6 telemetry. Never raises."""
    try:
        from groundtruth.runtime.v105_telemetry import log_index_freshness

        log_index_freshness(
            instance_id=instance_id,
            file=file_path,
            outcome=str(record.get("outcome", "")),
            elapsed_ms=record.get("elapsed_ms"),
            db_mtime_before=record.get("db_mtime_before"),
            db_mtime_after=record.get("db_mtime_after"),
            file_mtime=record.get("file_mtime"),
            pre_hash=str(record.get("pre_hash", "")),
            post_hash=str(record.get("post_hash", "")),
        )
    except Exception:
        pass


def any_stale(outcomes: dict[str, dict[str, Any]]) -> bool:
    """Returns True if any file's outcome indicates the graph.db is behind."""
    stale_set = {"stale", "stale_no_indexer", "timeout", "error", "no_db"}
    return any(o.get("outcome") in stale_set for o in outcomes.values())
