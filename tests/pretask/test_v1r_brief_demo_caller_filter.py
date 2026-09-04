"""v1r HOST brief docs/examples DEMO-path caller filter — red->green tests.

PROVEN LEAK (held-out VALIDATION py task `textual-richlog-follow-state`, run
fbdb1439): the brief's [CALLERS] pillar (_caller_contract_for_file) and the L1
`resolved caller:` annotation (_resolved_witnesses_for_file) rendered callers
located in DEMO paths (``docs/examples/`` / ``examples/``) straight into the
AGENT-DELIVERED context — they applied only ``_is_vendored_path`` and MISSED the
``_is_test_or_demo`` predicate the scope-chain (c15d306f) + localization entries
already enforce. delivered_instruction.txt leaked 8 refs like
``resolved caller: compose() in docs/examples/guide/input/key01.py:10`` and
``Callers: compose() in docs/examples/guide/input/key01.py``.

A ``docs/examples/`` caller is NOISE: it tells the agent to inspect a tutorial,
not a real dependent. ``_is_vendored_path`` does NOT catch ``examples/`` or
``docs/`` (its markers are ``/vendor/``, ``/node_modules/``, ``/dist/`` …) —
only ``_is_test_or_demo`` (whose demo segments include examples/example/docs/…)
does, so it is the load-bearing drop.

Correct-or-quiet contract proven here:
  - a caller in ``docs/examples/...`` is DROPPED from the [CALLERS] line and from
    the resolved-caller [WITNESS];
  - a callee in ``docs/examples/...`` is DROPPED from the resolved-call witness;
  - a TRUE ``src/...`` caller/callee SURVIVES (no over-suppression);
  - if ALL callers are demo, the [CALLERS] line is empty (never fabricated).

MUTATION-CHECK (proves the test BITES): set GT_DISABLE_DEMO_CALLER_FILTER=1 to
monkeypatch ``_is_test_or_demo`` to a constant-False stub at the v1r_brief module
seam, restoring the buggy pre-fix behaviour; the leak reappears and these asserts
go RED. With the fix live (filter present) they are GREEN.

All deterministic: sqlite fixtures, no network, no task IDs, no gold labels.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask import v1r_brief
from groundtruth.pretask.v1r_brief import (
    _caller_contract_for_file,
    _resolved_witnesses_for_file,
)

# Edit target lives in real source. The richlog shape: a widget defined in src/,
# called by a tutorial `compose()` in docs/examples/ AND by a real src/ caller.
_SRC_TARGET = "src/textual/widgets/_rich_log.py"  # defines `write`
_SRC_CALLER = "src/textual/widget.py"  # real src caller (must survive)
_DEMO_CALLER = "docs/examples/guide/input/key01.py"  # tutorial caller (must drop)
_EXAMPLES_CALLER = "examples/dictionary.py"  # top-level examples/ (must drop)
# callee side
_SRC_CALLEE = "src/textual/geometry.py"  # real src callee (must survive)
_DEMO_CALLEE = "docs/examples/widgets/log.py"  # demo callee (must drop)


def _create_graph_db(db_path: Path, nodes, edges) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
            name TEXT NOT NULL, qualified_name TEXT, file_path TEXT NOT NULL,
            start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL, type TEXT NOT NULL, source_line INTEGER,
            source_file TEXT, resolution_method TEXT, confidence REAL DEFAULT 1.0,
            metadata TEXT
        )
        """
    )
    key_to_id: dict[str, int] = {}
    for n in nodes:
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, signature, start_line, "
            "end_line, is_test, language) VALUES (?,?,?,?,?,?,?,?)",
            (
                n["label"],
                n["name"],
                n["file_path"],
                n.get("signature", ""),
                n.get("start_line", 1),
                n.get("end_line", 1),
                int(n.get("is_test", 0)),
                n.get("language", "python"),
            ),
        )
        key_to_id[n.get("key", n["name"])] = conn.execute("SELECT last_insert_rowid()").fetchone()[
            0
        ]
    for src, tgt, etype, line, method, conf in edges:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, source_line, "
            "resolution_method, confidence) VALUES (?,?,?,?,?,?)",
            (key_to_id[src], key_to_id[tgt], etype, line, method, conf),
        )
    conn.commit()
    conn.close()


def _write_repo_files(repo: Path) -> None:
    for rel in (
        _SRC_TARGET,
        _SRC_CALLER,
        _DEMO_CALLER,
        _EXAMPLES_CALLER,
        _SRC_CALLEE,
        _DEMO_CALLEE,
    ):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / _SRC_TARGET).write_text(
        "class RichLog:\n    def follow_end(self, animate=False):\n        return self._snap()\n",
        encoding="utf-8",
    )
    (repo / _SRC_CALLER).write_text(
        "def mount_log(self):\n    log = RichLog()\n    log.follow_end()\n", encoding="utf-8"
    )
    (repo / _DEMO_CALLER).write_text(
        "def compose(self):\n    rl = RichLog()\n    rl.follow_end()\n", encoding="utf-8"
    )
    (repo / _EXAMPLES_CALLER).write_text(
        "def compose(self):\n    rl = RichLog()\n    rl.follow_end()\n", encoding="utf-8"
    )
    (repo / _SRC_CALLEE).write_text("def Size(w, h):\n    return (w, h)\n", encoding="utf-8")
    (repo / _DEMO_CALLEE).write_text("def demo_helper():\n    return 1\n", encoding="utf-8")


_NODES = [
    {
        "label": "Method",
        "name": "follow_end",
        "key": "follow_end",
        "file_path": _SRC_TARGET,
        "signature": "def follow_end(self, animate=False)",
        "start_line": 2,
        "end_line": 3,
    },
    {
        "label": "Function",
        "name": "mount_log",
        "key": "mount_log",
        "file_path": _SRC_CALLER,
        "signature": "def mount_log(self)",
        "start_line": 1,
        "end_line": 3,
    },
    {
        "label": "Function",
        "name": "compose",
        "key": "compose_demo",
        "file_path": _DEMO_CALLER,
        "signature": "def compose(self)",
        "start_line": 1,
        "end_line": 2,
    },
    {
        "label": "Function",
        "name": "compose",
        "key": "compose_examples",
        "file_path": _EXAMPLES_CALLER,
        "signature": "def compose(self)",
        "start_line": 1,
        "end_line": 3,
    },
    {
        "label": "Function",
        "name": "Size",
        "key": "Size",
        "file_path": _SRC_CALLEE,
        "signature": "def Size(w, h)",
        "start_line": 1,
        "end_line": 2,
    },
    {
        "label": "Function",
        "name": "demo_helper",
        "key": "demo_helper",
        "file_path": _DEMO_CALLEE,
        "signature": "def demo_helper()",
        "start_line": 1,
        "end_line": 2,
    },
]

_EDGES = [
    # TRUE src caller (must survive) — deterministic provenance so it reaches the fact path
    ("mount_log", "follow_end", "CALLS", 3, "import", 1.0),
    # DEMO callers (must drop): tutorial compose() in docs/examples/ + examples/
    ("compose_demo", "follow_end", "CALLS", 3, "import", 1.0),
    ("compose_examples", "follow_end", "CALLS", 3, "import", 1.0),
    # TRUE src callee (must survive)
    ("follow_end", "Size", "CALLS", 3, "import", 1.0),
    # DEMO callee (must drop)
    ("follow_end", "demo_helper", "CALLS", 3, "import", 1.0),
]


@pytest.fixture
def richlog_repo(tmp_path: Path):
    db = tmp_path / "graph.db"
    repo = tmp_path / "proj"
    _write_repo_files(repo)
    _create_graph_db(db, _NODES, _EDGES)
    return repo, db


# ---------------------------------------------------------------------------
# MUTATION SEAM: restore the buggy pre-fix behaviour (no demo filter) so the
# test can prove it BITES. Patches the single module-level binding both render
# functions call, so it reverts BOTH fix sites at once.
# ---------------------------------------------------------------------------
@pytest.fixture
def disable_demo_filter(monkeypatch):
    monkeypatch.setattr(v1r_brief, "_is_test_or_demo", lambda _p: False)


# ===========================================================================
# [CALLERS] surface — _caller_contract_for_file  (the brief `Callers:` pillar)
# ===========================================================================
def test_demo_caller_dropped_from_callers_line(richlog_repo):
    repo, db = richlog_repo
    line = _caller_contract_for_file(str(db), _SRC_TARGET, str(repo), ["follow_end"])
    assert "docs/examples" not in line, f"demo caller leaked into Callers: {line}"
    assert "examples/dictionary.py" not in line, f"examples/ caller leaked: {line}"
    # the real src caller SURVIVES (correct-or-quiet keeps real facts)
    assert "mount_log() in " + _SRC_CALLER in line, f"true src caller lost: {line}"


def test_demo_caller_leaks_when_filter_disabled(richlog_repo, disable_demo_filter):
    """MUTATION CHECK: with the demo filter reverted, the leak reappears -> the
    above test would pass even on the buggy code only if it did NOT bite. This
    proves it bites."""
    repo, db = richlog_repo
    line = _caller_contract_for_file(str(db), _SRC_TARGET, str(repo), ["follow_end"])
    assert "docs/examples" in line, (
        f"mutation did not restore the leak — test does not bite: {line}"
    )


def test_all_demo_callers_yield_empty_callers_line(tmp_path):
    """Correct-or-quiet: when EVERY caller is a demo path, render NO Callers line
    (empty string), never a fabricated one."""
    db = tmp_path / "graph.db"
    repo = tmp_path / "proj"
    _write_repo_files(repo)
    edges = [
        ("compose_demo", "follow_end", "CALLS", 2, "import", 1.0),
        ("compose_examples", "follow_end", "CALLS", 3, "import", 1.0),
    ]
    _create_graph_db(db, _NODES, edges)
    line = _caller_contract_for_file(str(db), _SRC_TARGET, str(repo), ["follow_end"])
    assert line == "", f"all-demo callers must yield empty line, got: {line!r}"


# ===========================================================================
# [WITNESS] surface — _resolved_witnesses_for_file  (L1 `resolved caller:` line)
# ===========================================================================
def test_demo_caller_never_a_resolved_witness(richlog_repo):
    repo, db = richlog_repo
    wits = _resolved_witnesses_for_file(str(db), _SRC_TARGET, str(repo), max_each=4)
    rendered = " ".join(f"{w['file_path']} {w['symbol']} {w['target']}" for w in wits)
    assert "docs/examples" not in rendered, f"demo caller/callee witness leaked: {rendered}"
    assert "examples/dictionary.py" not in rendered, f"examples/ witness leaked: {rendered}"
    # true src caller AND callee survive
    assert any(w["direction"] == "caller" and w["file_path"] == _SRC_CALLER for w in wits), (
        f"true src caller witness over-suppressed: {wits}"
    )
    assert any(w["direction"] == "callee" and w["file_path"] == _SRC_CALLEE for w in wits), (
        f"true src callee witness over-suppressed: {wits}"
    )


def test_demo_witness_leaks_when_filter_disabled(richlog_repo, disable_demo_filter):
    """MUTATION CHECK for the WITNESS surface (L1 resolved-caller annotation)."""
    repo, db = richlog_repo
    wits = _resolved_witnesses_for_file(str(db), _SRC_TARGET, str(repo), max_each=4)
    rendered = " ".join(w["file_path"] for w in wits)
    assert "docs/examples" in rendered, (
        f"mutation did not restore the witness leak — does not bite: {rendered}"
    )
