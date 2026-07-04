"""Build a deterministic matched (graph.db, output.jsonl) fixture for shadow replay.

This is offline-only plumbing. It writes:

    <out_dir>/fixture/
        graph.db                                   — small CALLS+IMPORTS graph
        repo/...                                   — tiny source tree
        results/.../output.jsonl                   — synthetic agent trajectory

The trajectory deliberately exercises every router branch the timing tests
exercise in isolation: an unvisited-edge view (EMIT), a repeat view of the
same file (DUPLICATE or DEBOUNCE), an edit on a function with a real caller
(ON_EDIT_CONTRACT emit), an edit on a function with no caller (NO_EVIDENCE),
and a view late in iteration band (TOO_LATE).

This fixture exists because the archived ``output.jsonl`` artifacts in this
repo are not paired with their per-task graph.db. Once GHA artifact capture
saves graph.db alongside output.jsonl, this fixture can be replaced.

Usage:
    python scripts/build_replay_fixture.py --out reports/shadow_replay/fixture
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


_NODES: list[tuple] = [
    # id, label, name, qualified_name, file_path, start_line, end_line,
    # signature, return_type, is_exported, is_test, language, parent_id
    (1, "Function", "target", "core.target", "core/target.py", 1, 5, "def target(a, b)", "int", 1, 0, "python", 0),
    (2, "Function", "caller_one", "users.foo", "users/foo.py", 2, 4, "def caller_one()", None, 1, 0, "python", 0),
    (3, "Function", "caller_two", "users.bar", "users/bar.py", 2, 4, "def caller_two()", None, 1, 0, "python", 0),
    (4, "Function", "helper", "core.helper", "core/helper.py", 1, 3, "def helper()", "int", 1, 0, "python", 0),
    (5, "Function", "lonely", "extras.unused", "extras/unused.py", 1, 3, "def lonely()", None, 1, 0, "python", 0),
    (6, "Function", "test_target", "tests.test_core", "tests/test_core.py", 1, 4, "def test_target()", None, 0, 1, "python", 0),
]

_EDGES: list[tuple] = [
    # source_id, target_id, type, source_line, confidence
    (2, 1, "CALLS", 3, 1.0),  # users/foo.py -> core/target.py
    (3, 1, "CALLS", 3, 1.0),  # users/bar.py -> core/target.py
    (1, 4, "CALLS", 2, 1.0),  # core/target.py -> core/helper.py
    (4, 1, "IMPORTS", 0, 1.0),  # core/helper.py imports target
]


def _write_repo(repo_root: Path) -> None:
    """Create the tiny source tree referenced by graph.db nodes."""
    (repo_root / "core").mkdir(parents=True, exist_ok=True)
    (repo_root / "users").mkdir(parents=True, exist_ok=True)
    (repo_root / "extras").mkdir(parents=True, exist_ok=True)
    (repo_root / "tests").mkdir(parents=True, exist_ok=True)
    (repo_root / "core" / "target.py").write_text(
        "def target(a, b):\n    helper()\n    return a + b\n",
        encoding="utf-8",
    )
    (repo_root / "core" / "helper.py").write_text(
        "def helper():\n    return 1\n",
        encoding="utf-8",
    )
    (repo_root / "users" / "foo.py").write_text(
        "from core.target import target\n"
        "def caller_one():\n"
        "    return target(1, 2)\n",
        encoding="utf-8",
    )
    (repo_root / "users" / "bar.py").write_text(
        "from core.target import target\n"
        "def caller_two():\n"
        "    return target(3, 4)\n",
        encoding="utf-8",
    )
    (repo_root / "extras" / "unused.py").write_text(
        "def lonely():\n    return 0\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "test_core.py").write_text(
        "def test_target():\n    assert target(1, 2) == 3\n",
        encoding="utf-8",
    )


def _write_graph_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(str(db_path))
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT,
            is_exported INTEGER, is_test INTEGER,
            language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.5,
            metadata TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _NODES,
    )
    for src, tgt, etype, line, conf in _EDGES:
        con.execute(
            "INSERT INTO edges (source_id, target_id, type, source_line, confidence) VALUES (?, ?, ?, ?, ?)",
            (src, tgt, etype, line, conf),
        )
    con.commit()
    con.close()


def _make_trace(workspace_root: str = "/workspace/fixture") -> dict:
    """Build a small but realistic output.jsonl document.

    The trajectory deliberately spaces same-kind actions far enough apart to
    exercise DUPLICATE / NO_EVIDENCE / BUDGET / STALE / TOO_LATE / EMIT, not
    only DEBOUNCE. Filler ``run`` events sit between router-relevant actions.
    """
    workspace = workspace_root.rstrip("/")
    iso = "2026-05-17T10:00:00"
    history: list[dict] = []
    next_id = 1

    def add(action: str, args: dict, message: str = "") -> None:
        nonlocal next_id
        history.append({
            "id": next_id,
            "timestamp": iso,
            "source": "agent",
            "action": action,
            "args": args,
            "message": message,
        })
        next_id += 1

    def add_obs(message: str) -> None:
        nonlocal next_id
        history.append({
            "id": next_id,
            "timestamp": iso,
            "source": "user",
            "message": message,
            "action": "",
            "args": {},
        })
        next_id += 1

    def filler(n: int = 4) -> None:
        # ``run`` actions are NOT consumed by the router but DO count as
        # actions in ``action_count`` (per localization_metrics rules), so 4
        # of them advance the iter past the 3-iter debounce window.
        for _ in range(n):
            add("run", {"command": "echo waiting"}, message="filler")
            add_obs("ok\n")

    # 1) Read core/target.py for the first time. Router should EMIT.
    add("read", {"path": f"{workspace}/core/target.py", "start": 0, "end": -1})
    add_obs("file contents\n")
    filler(4)

    # 2) Read core/target.py AGAIN, past debounce window. Expect DUPLICATE
    #    (same target+primary already in dedup set).
    add("read", {"path": f"{workspace}/core/target.py", "start": 0, "end": -1})
    add_obs("file contents\n")
    filler(4)

    # 3) Read extras/unused.py which has zero graph edges. Expect NO_EVIDENCE.
    add("read", {"path": f"{workspace}/extras/unused.py", "start": 0, "end": -1})
    add_obs("[GT] old hook fired here (proxy marker)\n")
    filler(4)

    # 4) Edit core/target.py with function ``target`` (has caller). Expect EMIT
    #    on ON_EDIT_CONTRACT (different kind from ON_VIEW so debounce doesn't apply).
    add(
        "edit",
        {
            "path": f"{workspace}/core/target.py",
            "command": "str_replace",
            "old_str": "def target(a, b):\n    helper()\n    return a + b",
            "new_str": "def target(a, b):\n    helper()\n    return a + b + 1",
        },
        message="editing target",
    )
    add_obs("file edited\n")
    filler(4)

    # 5) Edit core/target.py again past debounce. Expect DUPLICATE on edit kind.
    add(
        "edit",
        {
            "path": f"{workspace}/core/target.py",
            "command": "str_replace",
            "old_str": "return a + b + 1",
            "new_str": "return a + b + 2",
        },
        message="re-editing target",
    )
    add_obs("file edited\n")
    filler(4)

    # 6) Edit users/foo.py with caller_one (no caller in the graph -> NO_EVIDENCE).
    add(
        "edit",
        {
            "path": f"{workspace}/users/foo.py",
            "command": "str_replace",
            "old_str": "def caller_one():\n    return target(1, 2)",
            "new_str": "def caller_one():\n    return target(1, 3)",
        },
        message="editing caller_one",
    )
    add_obs("file edited\n")
    filler(4)

    # 7) Read users/bar.py past debounce. Provider has callers (target is the
    #    target of foo+bar). Router emits.
    add("read", {"path": f"{workspace}/users/bar.py", "start": 0, "end": -1})
    add_obs("file contents\n")
    filler(4)

    # 8) Late-band read: actions are now ~32+. With max_iter=20 the late_band
    #    ratio (>=0.75 of 20 = 15) is well exceeded -> TOO_LATE.
    add("read", {"path": f"{workspace}/core/helper.py", "start": 0, "end": -1})
    add_obs("file contents\n")

    return {
        "instance_id": "fixture__small",
        "instance": {"instance_id": "fixture__small"},
        "metadata": {"max_iterations": 20},
        "instruction": (
            "Fix the bug.\n"
            "<gt-task-brief>\n"
            "1. core/target.py — target\n"
            "</gt-task-brief>\n"
        ),
        "history": history,
        "test_result": {
            "git_patch": "diff --git a/core/target.py b/core/target.py\n+++ b/core/target.py\n@@ ...\n",
        },
        "metrics": {},
        "error": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="reports/shadow_replay/fixture")
    parser.add_argument("--workspace", default="/workspace/fixture")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    repo_root = out_dir / "repo"
    _write_repo(repo_root)
    db_path = out_dir / "graph.db"
    _write_graph_db(db_path)

    results_dir = out_dir / "results" / "SWE-bench-Live__SWE-bench-Live-lite" / "CodeActAgent" / "deepseek-v4-flash_maxiter_100"
    results_dir.mkdir(parents=True, exist_ok=True)
    trace_path = results_dir / "output.jsonl"
    trace_path.write_text(
        json.dumps(_make_trace(args.workspace)) + "\n",
        encoding="utf-8",
    )

    map_path = out_dir / "graph_map.json"
    map_path.write_text(
        json.dumps({"fixture__small": str(db_path.resolve())}, indent=2),
        encoding="utf-8",
    )

    print(f"Wrote fixture under {out_dir}:")
    print(f"  graph.db          = {db_path}")
    print(f"  output.jsonl      = {trace_path}")
    print(f"  graph_map.json    = {map_path}")
    print(f"  repo              = {repo_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
