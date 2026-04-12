from __future__ import annotations

import os
import time
from pathlib import Path

from groundtruth.index.store import SymbolStore
from groundtruth.mcp.endpoints.server_v2 import _resolve_tool_symbol_target
from groundtruth.mcp.freshness_gate import FreshnessGate


def _setup_store(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".groundtruth").mkdir()
    db_path = root / ".groundtruth" / "index.db"
    store = SymbolStore(str(db_path))
    store.initialize()
    return root, db_path, store


def test_symbol_target_resolution_abstains_when_ambiguous(tmp_path: Path) -> None:
    root, _, store = _setup_store(tmp_path)
    now = int(time.time())
    store.insert_symbol("dup", "function", "python", "src/a.py", 1, 3, True, None, None, None, None, last_indexed_at=now)
    store.insert_symbol("dup", "function", "python", "src/b.py", 1, 3, True, None, None, None, None, last_indexed_at=now)

    result = _resolve_tool_symbol_target(store, "gt_lookup", {"symbol": "dup"})

    assert result["status"] == "ambiguous"
    assert sorted(result["matches"]) == ["src/a.py", "src/b.py"]


def test_symbol_target_resolution_returns_file_for_unique_symbol(tmp_path: Path) -> None:
    _, _, store = _setup_store(tmp_path)
    now = int(time.time())
    store.insert_symbol("foo", "function", "python", "src/foo.py", 1, 3, True, None, None, None, None, last_indexed_at=now)

    result = _resolve_tool_symbol_target(store, "gt_lookup", {"symbol": "foo"})

    assert result["status"] == "resolved"
    assert result["file_path"] == "src/foo.py"


def test_freshness_gate_marks_resolved_symbol_file_stale(tmp_path: Path) -> None:
    root, db_path, _ = _setup_store(tmp_path)
    source_file = root / "src" / "foo.py"
    source_file.parent.mkdir()
    source_file.write_text("def foo():\n    return 1\n", encoding="utf-8")

    graph_time = time.time() - 1
    os.utime(db_path, (graph_time, graph_time))
    file_time = time.time() + 20
    os.utime(source_file, (file_time, file_time))

    gate = FreshnessGate(str(db_path), str(root))
    verdict = gate.check("src/foo.py")

    assert verdict.should_downgrade is True
    assert verdict.should_suppress is False
    assert "outdated" in verdict.reason.lower() or "modified" in verdict.reason.lower()


def test_freshness_gate_suppresses_when_graph_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    gate = FreshnessGate(str(root / ".groundtruth" / "missing.db"), str(root))
    verdict = gate.check("src/foo.py")

    assert verdict.should_suppress is True
    assert "graph.db not found" in verdict.reason


def test_freshness_gate_allows_fresh_symbol_file(tmp_path: Path) -> None:
    root, db_path, _ = _setup_store(tmp_path)
    source_file = root / "src" / "foo.py"
    source_file.parent.mkdir()
    source_file.write_text("def foo():\n    return 1\n", encoding="utf-8")

    current = time.time()
    os.utime(db_path, (current, current))
    os.utime(source_file, (current - 1, current - 1))

    gate = FreshnessGate(str(db_path), str(root))
    verdict = gate.check("src/foo.py")

    assert verdict.is_fresh is True


def test_freshness_gate_suppresses_when_graph_is_old(tmp_path: Path) -> None:
    root, db_path, _ = _setup_store(tmp_path)
    source_file = root / "src" / "foo.py"
    source_file.parent.mkdir()
    source_file.write_text("def foo():\n    return 1\n", encoding="utf-8")

    graph_time = time.time() - (FreshnessGate.STALE_THRESHOLD + 10)
    os.utime(db_path, (graph_time, graph_time))

    gate = FreshnessGate(str(db_path), str(root))
    verdict = gate.check("src/foo.py")

    assert verdict.should_suppress is True
    assert "old" in verdict.reason.lower() or "abstaining" in verdict.reason.lower()
