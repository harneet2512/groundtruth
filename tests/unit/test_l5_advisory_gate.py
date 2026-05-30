"""TTD — TASK #46: gate the L5 "Unexamined structural signal" advisory.

Artifact-first reference (real beets trajectory, ev31/ev538):
  GT emitted: "[GT L5: Unexamined structural signal] A high-confidence structural
  relation involving {file} has not been examined. It may be relevant to the edit."
  - It named only a FILE (no specific symbol/relation).
  - It fired unconditionally — NOT gated on a stuck state.
  - It fired AFTER the agent had already self-localized -> redundant noise.

Required behavior (correct-or-quiet):
  - The advisory may fire ONLY when the agent is in a proven-stuck state
    (the obs-fingerprint repeat / STUCK_COMPAT signal), AND
  - it must name a SPECIFIC symbol/relation (a function/edge), not just a file.
  - If not stuck OR no specific symbol -> SILENCE (return None).

This test builds a minimal temp graph.db fixture (NOT derived from reading the
implementation) and drives the gate helpers directly:
  (red)   helpers absent / advisory fires unconditionally and vaguely
  (green) suppressed when not stuck; fires naming the symbol when stuck + symbol present
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "swebench"))
sys.modules.setdefault(
    "litellm",
    SimpleNamespace(
        model_cost={}, success_callback=[],
        completion=lambda *a, **k: None, acompletion=None,
        completion_cost=lambda *a, **k: 0.0,
    ),
)

from scripts.swebench import oh_gt_full_wrapper as ohgt  # noqa: E402


def _build_relation_db(path: str) -> None:
    """importer.py defines set_fields; ui/commands.py::run_import CALLS it (cross-file)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
            type TEXT NOT NULL, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
        );
        """
    )
    nodes = [
        ("Method", "set_fields", "beets/importer.py", 600),   # id 1 (edited symbol)
        ("Function", "run_import", "beets/ui/commands.py", 300),  # id 2 (caller, the unexamined relation)
    ]
    for label, name, fp, line in nodes:
        conn.execute(
            "INSERT INTO nodes (label, name, qualified_name, file_path, start_line, "
            "end_line, signature, return_type, is_exported, is_test, language) "
            "VALUES (?,?,?,?,?,?,'','',0,0,'python')",
            (label, name, name, fp, line, line + 10),
        )
    # run_import (id 2) CALLS set_fields (id 1) — high confidence, verified.
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
        "resolution_method, confidence) VALUES (2,1,'CALLS',320,'beets/ui/commands.py','import',1.0)"
    )
    conn.commit()
    conn.close()


def _make_config(stuck_history, edited_files, db_path):
    """Minimal config stand-in carrying the fields the gate reads."""
    return SimpleNamespace(
        _stuck_compat_history=list(stuck_history),
        _stuck_compat_skip_count=0,
        edited_files=set(edited_files),
        _host_graph_db=db_path,
        graph_db="",
        workspace_root="",
        action_count=20,
        max_iter=100,
    )


def test_stuck_helper_exists_and_is_false_on_fresh_history():
    assert hasattr(ohgt, "_is_agent_stuck"), "fix must add _is_agent_stuck(config)"
    cfg = _make_config([], [], "")
    assert ohgt._is_agent_stuck(cfg) is False


def test_stuck_helper_true_on_repeated_action_fingerprint():
    cfg = _make_config([], [], "")
    # Same action fingerprint repeated -> proven stuck.
    fp = ("CmdRunAction:cat importer.py", "deadbeef")
    cfg._stuck_compat_history = [fp, ("CmdRunAction:ls", "aaaa"), fp]
    assert ohgt._is_agent_stuck(cfg) is True


def test_advisory_suppressed_when_not_stuck(tmp_path):
    """Not stuck -> advisory stays silent even when a specific relation exists."""
    assert hasattr(ohgt, "_l5_advisory_message"), (
        "fix must add _l5_advisory_message(config, naf) -> str|None"
    )
    db = str(tmp_path / "graph.db")
    _build_relation_db(db)
    cfg = _make_config([], ["beets/importer.py"], db)  # fresh history = not stuck
    msg = ohgt._l5_advisory_message(cfg, "beets/ui/commands.py")
    assert msg is None, f"not-stuck must suppress, got: {msg!r}"


def test_advisory_fires_naming_symbol_when_stuck(tmp_path):
    """Stuck + a resolvable specific symbol -> advisory fires and NAMES the symbol."""
    db = str(tmp_path / "graph.db")
    _build_relation_db(db)
    fp = ("CmdRunAction:cat importer.py", "deadbeef")
    cfg = _make_config([fp, ("x", "y"), fp], ["beets/importer.py"], db)
    msg = ohgt._l5_advisory_message(cfg, "beets/ui/commands.py")
    assert msg is not None, "stuck + specific symbol must fire"
    # Must name the SPECIFIC symbol/relation, not merely the file.
    assert "set_fields" in msg or "run_import" in msg, (
        f"advisory must name a specific symbol, got: {msg!r}"
    )


def test_advisory_silent_when_stuck_but_no_specific_symbol(tmp_path):
    """Stuck but no resolvable concrete relation -> silence (no vague file-only line)."""
    db = str(tmp_path / "graph.db")
    _build_relation_db(db)
    fp = ("CmdRunAction:cat importer.py", "deadbeef")
    cfg = _make_config([fp, ("x", "y"), fp], ["beets/importer.py"], db)
    # naf has no edge relation to the edited file in the graph -> no concrete symbol.
    msg = ohgt._l5_advisory_message(cfg, "beets/totally_unrelated.py")
    assert msg is None, f"no concrete symbol must yield silence, got: {msg!r}"
