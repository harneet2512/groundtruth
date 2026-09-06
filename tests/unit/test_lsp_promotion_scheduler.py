from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
from pathlib import Path

from groundtruth.lsp.background_promotion import (
    LSPPromotionRequest,
    LSPPromotionScheduler,
    repository_snapshot_sha256,
)


def _graph(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE nodes(id INTEGER PRIMARY KEY, language TEXT);
            CREATE TABLE edges(
                id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
                type TEXT, resolution_method TEXT, confidence REAL
            );
            INSERT INTO nodes VALUES(1, 'python'), (2, 'python');
            INSERT INTO edges VALUES(1, 1, 2, 'CALLS', 'name_match', 0.5);
            """
        )
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(graph: Path, candidate: Path) -> LSPPromotionRequest:
    repository = graph.parent / "repository-snapshot"
    repository.mkdir(exist_ok=True)
    (repository / "module.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    return LSPPromotionRequest(
        source_revision="source-rev-1",
        graph_revision="graph-rev-1",
        graph_path=str(graph),
        graph_sha256=_digest(graph),
        repository_root=str(repository),
        repository_snapshot_sha256=repository_snapshot_sha256(repository),
        candidate_path=str(candidate),
    )


def test_scheduler_enriches_a_copy_and_returns_revision_bound_terminal_receipt(tmp_path):
    graph = _graph(tmp_path / "graph.db")
    candidate = tmp_path / "candidate.db"
    before = graph.read_bytes()

    async def resolve(db_path, _root, edges, language):
        assert language == "python" and len(edges) == 1
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE edges SET confidence=1.0,resolution_method='lsp' WHERE id=1")
        return {"verified": 1, "corrected": 0, "deleted": 0, "failed": 0}

    scheduler = LSPPromotionScheduler(
        server_detector=lambda: {"python": "pyright-langserver"},
        edge_loader=lambda _path, _language: [{"id": 1}],
        resolver=resolve,
        closure_rebuilder=lambda _path: True,
    )
    try:
        handle = scheduler.schedule(_request(graph, candidate))
        receipt = handle.terminal_receipt(timeout=5)
    finally:
        scheduler.close()

    assert receipt["status"] == "succeeded"
    assert receipt["source_revision"] == "source-rev-1"
    assert receipt["input_graph_revision"] == "graph-rev-1"
    assert receipt["input_graph_sha256"] == _digest(graph)
    assert receipt["output_graph_sha256"] == _digest(candidate)
    assert receipt["languages_attempted"] == ["python"]
    assert receipt["languages_unavailable"] == []
    assert receipt["all_promotable_languages_attempted"] is True
    assert receipt["repository_snapshot_sha256"] == repository_snapshot_sha256(
        graph.parent / "repository-snapshot"
    )
    assert receipt["language_fidelity"] == {
        "python": {
            "detected_executable": "pyright-langserver",
            "scope": "definition_resolution_for_eligible_ambiguous_calls",
        }
    }
    assert receipt["language_receipts"]["python"]["verified"] == 1
    assert receipt["verified"] == 1
    assert receipt["closure_rebuilt"] is True
    assert graph.read_bytes() == before
    assert not Path(f"{candidate}-wal").exists()
    assert not Path(f"{candidate}-shm").exists()
    with sqlite3.connect(candidate) as connection:
        assert (
            connection.execute("SELECT resolution_method FROM edges WHERE id=1").fetchone()[0]
            == "lsp"
        )


def test_scheduler_cancellation_never_leaves_a_publishable_candidate(tmp_path):
    graph = _graph(tmp_path / "graph.db")
    candidate = tmp_path / "candidate.db"
    entered = threading.Event()

    async def resolve(_db_path, _root, _edges, _language):
        entered.set()
        await asyncio.sleep(0.05)
        return {"verified": 0, "corrected": 0, "deleted": 0, "failed": 0}

    scheduler = LSPPromotionScheduler(
        server_detector=lambda: {"python": "pyright-langserver"},
        edge_loader=lambda _path, _language: [{"id": 1}],
        resolver=resolve,
    )
    try:
        handle = scheduler.schedule(_request(graph, candidate))
        assert entered.wait(5)
        assert handle.cancel() is True
        receipt = handle.terminal_receipt(timeout=5)
    finally:
        scheduler.close()

    assert receipt["status"] == "cancelled"
    assert receipt["publishable"] is False
    assert not candidate.exists()
    assert graph.is_file()


def test_scheduler_reports_declared_language_fidelity_without_claiming_execution(tmp_path):
    graph = _graph(tmp_path / "graph.db")
    candidate = tmp_path / "candidate.db"
    scheduler = LSPPromotionScheduler(
        server_detector=lambda: {},
        edge_loader=lambda _path, _language: [{"id": 1}],
    )
    try:
        receipt = scheduler.schedule(_request(graph, candidate)).terminal_receipt(timeout=5)
    finally:
        scheduler.close()

    assert receipt["status"] == "unavailable"
    assert receipt["reason"] == "no_language_server_for_promotable_languages"
    assert receipt["languages_promotable"] == ["python"]
    assert receipt["languages_attempted"] == []
    assert receipt["languages_unavailable"] == ["python"]
    assert receipt["all_promotable_languages_attempted"] is False
    assert receipt["language_fidelity"] == {}
    assert receipt["publishable"] is False
    assert not candidate.exists()


def test_scheduler_rejects_a_source_snapshot_changed_after_request(tmp_path):
    graph = _graph(tmp_path / "graph.db")
    candidate = tmp_path / "candidate.db"
    request = _request(graph, candidate)
    (Path(request.repository_root) / "module.py").write_text(
        "def replacement():\n    return 2\n", encoding="utf-8"
    )
    scheduler = LSPPromotionScheduler(
        server_detector=lambda: {"python": "pyright-langserver"},
        edge_loader=lambda _path, _language: [{"id": 1}],
    )
    try:
        receipt = scheduler.schedule(request).terminal_receipt(timeout=5)
    finally:
        scheduler.close()

    assert receipt["status"] == "failed"
    assert receipt["reason"] == ("ValueError:lsp_promotion_repository_snapshot_changed")
    assert receipt["publishable"] is False
    assert not candidate.exists()


def test_interrupted_language_pass_receipt_does_not_claim_queued_languages(tmp_path):
    graph = _graph(tmp_path / "graph.db")
    with sqlite3.connect(graph) as connection:
        connection.executescript(
            "INSERT INTO nodes VALUES(3, 'typescript');"
            "INSERT INTO edges VALUES(2, 3, 2, 'CALLS', 'name_match', 0.5);"
        )

    async def resolve(_db_path, _root, _edges, language):
        assert language == "python"
        raise RuntimeError("language_pass_interrupted")

    scheduler = LSPPromotionScheduler(
        server_detector=lambda: {
            "python": "pyright-langserver",
            "typescript": "typescript-language-server",
        },
        edge_loader=lambda _path, _language: [{"id": 1}],
        resolver=resolve,
    )
    try:
        receipt = scheduler.schedule(_request(graph, tmp_path / "candidate.db")).terminal_receipt(
            timeout=5
        )
    finally:
        scheduler.close()
    assert receipt["status"] == "failed"
    assert receipt["languages_attempted"] == ["python"]
    assert receipt["languages_completed"] == []
    assert receipt["all_promotable_languages_attempted"] is False
    assert receipt["language_receipts"]["python"]["status"] == "failed"
    assert not receipt["publishable"]


def test_edge_loader_closes_its_read_connection_without_gc(tmp_path, monkeypatch):
    graph = _graph(tmp_path / "graph.db")
    real_connect = sqlite3.connect
    close_calls = []
    monkeypatch.setattr(
        "groundtruth.resolve._get_ambiguous_edges",
        lambda _connection, **_kwargs: [{"id": 1}],
    )

    class ConnectionProxy:
        def __init__(self, *args, **kwargs):
            self.inner = real_connect(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def close(self):
            close_calls.append(True)
            self.inner.close()

    monkeypatch.setattr(sqlite3, "connect", ConnectionProxy)

    edges = LSPPromotionScheduler._load_edges(str(graph), "python")

    assert edges == [{"id": 1}]
    assert close_calls == [True]
