"""DeepSWE delivery-surface parity fixes — red->green tests (4-reviewer LIPI audit).

The unify inlined the host pillars into `artifact_deepswe/gt_mini_patch.py` but
diverged from canon. These tests pin the restored parity:

  #1 Basename-LIKE wrong-file evidence : `%__init__.py` matched EVERY package's
     __init__ — evidence from OTHER files rendered as facts. Canon (v1r
     `_top_function_names`) is EXACT `n.file_path = ?` on the normalized relpath.
  #2 Ungated caller counts            : `_graph_contract_block`'s COUNT had no
     deterministic-method gate / confidence floor / is_test=0 — name_match
     laundered into a "N caller(s)" number. Canon: curation_map
     `_verified_neighbor_count` (legacy schema -> ABSTAIN, no fake number).
  #3 Legacy-schema callee laundering  : `_edit_target_callee_contracts` dropped
     the deterministic filter when resolution_method was absent. Canon:
     contract_map.edit_target_callee_contracts re-gates per row; legacy -> abstain.
  #4 Missing sanitizers               : raw `n.signature` + raw property values
     rendered without `_sanitize_signature` (pyright hover-markdown D-2) or the
     balanced value-clip. Canon: contract_map._sanitize_signature + clip_balanced.
  #5 WAL + ro mount + plain connect   : `sqlite3.connect(db)` against the ro bind
     mount can fail on WAL graphs -> every pillar silently "". Fix: URI mode=ro
     (immutable=1 on the truly-ro substrate mount) + a ONE-TIME readability probe
     that prints a single classified line, then stays quiet.
  #6 GT_BASELINE truthy-parse         : `bool(os.environ.get(...))` made
     GT_BASELINE=0 enable the baseline arm. Fix: strict == "1".
  #7 Adapter raise visibility         : DeepSweAdapterError raised bare — pier
     swallows it and the workflow grep / outcome classifier never see it. Fix:
     print `[GT_META] ... error=DEEPSWE_ADAPTER_FAIL detail=<class>` BEFORE every
     raise; witness + brief raise scope CONSISTENT: (proof OR substrate).
  #8 yaml overclaim                   : "verified cross-file facts" -> honest
     description + one-line explanations for the other GT tags.
  #9 (unverified) tag                 : bare name_match caller hints under
     [CALLERS] now carry "(unverified)" (curation_map._fmt_edge discipline).

All deterministic: sqlite fixtures, no Go toolchain, no network, no task IDs.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_AGENT_PATH = _ROOT / "artifact_deepswe" / "gt_agent.py"
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"
_PIER_CFG = _ROOT / "artifact_deepswe" / "gt_integration" / "deepswe_gt_pier.yaml"

_load_count = 0


def _load(path: Path, name_prefix: str):
    """Fresh module instance per call (module-level state isolated per test)."""
    global _load_count
    _load_count += 1
    name = f"{name_prefix}_{_load_count}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _gt_env_clear(monkeypatch):
    for k in list(os.environ):
        if k.startswith("GT_"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def patch_mod(monkeypatch):
    _gt_env_clear(monkeypatch)
    return _load(_PATCH_PATH, "gt_mini_patch_dsf")


# ---------------------------------------------------------------------------
# graph.db fixture builder (Go-indexer output schema)
# ---------------------------------------------------------------------------
def _create_graph_db(db_path: Path, nodes: list[dict], edges: list[tuple],
                     *, with_method_cols: bool = True,
                     cochanges: list[tuple] | None = None) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, name TEXT NOT NULL,
            qualified_name TEXT, file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL DEFAULT 'python', parent_id INTEGER
        )
        """
    )
    if with_method_cols:
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
    else:
        # Legacy schema: NO resolution_method, NO confidence.
        conn.execute(
            """
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL, type TEXT NOT NULL, source_line INTEGER,
                source_file TEXT, metadata TEXT
            )
            """
        )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS properties (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "node_id INTEGER, kind TEXT, value TEXT, line INTEGER)"
    )
    if cochanges is not None:
        conn.execute(
            "CREATE TABLE cochanges (file_a TEXT, file_b TEXT, count INTEGER)"
        )
        conn.executemany("INSERT INTO cochanges VALUES (?,?,?)", cochanges)
    key_to_id: dict[str, int] = {}
    for n in nodes:
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, signature, start_line, end_line, "
            "is_test, language) VALUES (?,?,?,?,?,?,?,?)",
            (n["label"], n["name"], n["file_path"], n.get("signature", ""),
             n.get("start_line", 1), n.get("end_line", 1), int(n.get("is_test", 0)),
             n.get("language", "python")),
        )
        key = n.get("key", n["name"])
        key_to_id[key] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for e in edges:
        if with_method_cols:
            src, tgt, etype, line, method, conf = e
            conn.execute(
                "INSERT INTO edges (source_id, target_id, type, source_line, "
                "resolution_method, confidence) VALUES (?,?,?,?,?,?)",
                (key_to_id[src], key_to_id[tgt], etype, line, method, conf),
            )
        else:
            src, tgt, etype, line = e[:4]
            conn.execute(
                "INSERT INTO edges (source_id, target_id, type, source_line) "
                "VALUES (?,?,?,?)",
                (key_to_id[src], key_to_id[tgt], etype, line),
            )
    conn.commit()
    conn.close()
    # let callers add properties later via key_to_id if needed
    _create_graph_db.last_ids = key_to_id  # type: ignore[attr-defined]


# ===========================================================================
# BUG #1 — basename-LIKE must not cross-attribute between two __init__.py files
# ===========================================================================
@pytest.fixture
def two_init_repo(tmp_path: Path):
    """pkg_a/__init__.py (alpha_func, NO callers) and pkg_b/__init__.py
    (beta_func, 1 deterministic caller from main.py + a co-change with main.py).
    Nothing from pkg_b may ever be attributed to pkg_a."""
    db = tmp_path / "graph.db"
    nodes = [
        {"label": "Function", "name": "alpha_func", "file_path": "pkg_a/__init__.py",
         "signature": "def alpha_func(x)", "start_line": 1, "end_line": 3},
        {"label": "Function", "name": "beta_func", "file_path": "pkg_b/__init__.py",
         "signature": "def beta_func(y)", "start_line": 1, "end_line": 3},
        {"label": "Function", "name": "run", "file_path": "main.py",
         "signature": "def run()", "start_line": 1, "end_line": 5},
    ]
    edges = [
        ("run", "beta_func", "CALLS", 3, "import", 1.0),
    ]
    _create_graph_db(db, nodes, edges,
                     cochanges=[("pkg_b/__init__.py", "main.py", 5)])
    return tmp_path, db


def test_bug1_top_func_names_no_cross_attribution(two_init_repo, patch_mod):
    _tmp, db = two_init_repo
    con = sqlite3.connect(str(db))
    try:
        names = patch_mod._top_func_names(con, "pkg_a/__init__.py", limit=5)
        assert "alpha_func" in names, f"own function missing: {names}"
        assert "beta_func" not in names, (
            f"BUG1: beta_func (pkg_b/__init__.py) cross-attributed to pkg_a: {names}"
        )
    finally:
        con.close()


def test_bug1_top_func_names_normalizes_dot_slash(two_init_repo, patch_mod):
    """Exact match must still hit after `./` / `\\` normalization (the graph
    stores forward-slash relpaths — gt-index walker.go ToSlash)."""
    _tmp, db = two_init_repo
    con = sqlite3.connect(str(db))
    try:
        assert patch_mod._top_func_names(con, "./pkg_a/__init__.py") == ["alpha_func"]
        assert patch_mod._top_func_names(con, "pkg_a\\__init__.py") == ["alpha_func"]
    finally:
        con.close()


def test_bug1_sibling_context_no_cross_attribution(two_init_repo, patch_mod):
    _tmp, db = two_init_repo
    con = sqlite3.connect(str(db))
    try:
        sib = patch_mod._sibling_context(con, "pkg_a/__init__.py", ["alpha_func"])
        assert "beta_func" not in sib, (
            f"BUG1: sibling from ANOTHER package's __init__.py rendered: {sib!r}"
        )
    finally:
        con.close()


def test_bug1_graph_contract_block_no_cross_attribution(two_init_repo, patch_mod, monkeypatch):
    _tmp, db = two_init_repo
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    block = patch_mod._graph_contract_block("pkg_a/__init__.py")
    # pkg_a's own signature may render; pkg_b's must NEVER.
    assert "beta_func" not in block, (
        f"BUG1: <gt-contract> for pkg_a/__init__.py contains pkg_b evidence:\n{block}"
    )


def test_bug1_query_scope_no_cross_attribution(two_init_repo, patch_mod, monkeypatch):
    _tmp, db = two_init_repo
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    scope = patch_mod._query_scope("pkg_a/__init__.py")
    assert "main.py" not in scope, (
        f"BUG1: scope for pkg_a/__init__.py pulled pkg_b's neighbor main.py: {scope}"
    )


def test_bug1_consensus_block_no_cross_attribution(two_init_repo, patch_mod, monkeypatch):
    _tmp, db = two_init_repo
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    block = patch_mod._consensus_block("pkg_a/__init__.py", str(_tmp))
    assert "main.py" not in block, (
        f"BUG1: consensus scope for pkg_a cross-attributed pkg_b's neighbor:\n{block}"
    )


def test_bug1_cochange_block_no_cross_attribution(two_init_repo, patch_mod, monkeypatch):
    _tmp, db = two_init_repo
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    block = patch_mod._cochange_block("pkg_a/__init__.py")
    assert block == "", (
        f"BUG1: co-change of pkg_b/__init__.py attributed to pkg_a/__init__.py:\n{block}"
    )
    # And the REAL co-change partner still fires for pkg_b (no over-suppression).
    block_b = patch_mod._cochange_block("pkg_b/__init__.py")
    assert "main.py" in block_b, f"pkg_b's genuine co-change lost:\n{block_b}"


# ===========================================================================
# BUG #2 — caller COUNT gated: deterministic method + confidence>=0.7 + is_test=0
# ===========================================================================
@pytest.fixture
def caller_count_repo(tmp_path: Path):
    """lib/util.py::helper with 4 incoming CALLS edges:
       - app/a.py::use_a    import / 1.0 / is_test=0  -> COUNTS
       - app/b.py::use_b    name_match / 0.9          -> excluded (not deterministic)
       - tests/t.py::test_h import / 1.0 / is_test=1  -> excluded (test caller)
       - app/c.py::use_c    import / 0.4              -> excluded (confidence < 0.7)
    """
    db = tmp_path / "graph.db"
    nodes = [
        {"label": "Function", "name": "helper", "file_path": "lib/util.py",
         "signature": "def helper(v)", "start_line": 1, "end_line": 4},
        {"label": "Function", "name": "use_a", "file_path": "app/a.py"},
        {"label": "Function", "name": "use_b", "file_path": "app/b.py"},
        {"label": "Function", "name": "test_h", "file_path": "tests/t.py", "is_test": 1},
        {"label": "Function", "name": "use_c", "file_path": "app/c.py"},
    ]
    edges = [
        ("use_a", "helper", "CALLS", 2, "import", 1.0),
        ("use_b", "helper", "CALLS", 3, "name_match", 0.9),
        ("test_h", "helper", "CALLS", 4, "import", 1.0),
        ("use_c", "helper", "CALLS", 5, "import", 0.4),
    ]
    _create_graph_db(db, nodes, edges)
    return tmp_path, db


def test_bug2_caller_count_excludes_name_match_test_and_lowconf(
        caller_count_repo, patch_mod, monkeypatch):
    _tmp, db = caller_count_repo
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    block = patch_mod._graph_contract_block("lib/util.py")
    assert block, "expected a <gt-contract> block for lib/util.py"
    assert "1 verified caller(s) in 1 file(s)" in block, (
        f"BUG2: caller count not gated (name_match / test / low-conf laundered):\n{block}"
    )


def test_bug2_caller_count_abstains_on_legacy_schema(tmp_path, patch_mod, monkeypatch):
    """No resolution_method column -> provenance unknowable -> NO count line at
    all (no number rather than a fake one). The signature itself still renders."""
    db = tmp_path / "legacy.db"
    nodes = [
        {"label": "Function", "name": "helper", "file_path": "lib/util.py",
         "signature": "def helper(v)"},
        {"label": "Function", "name": "use_a", "file_path": "app/a.py"},
    ]
    edges = [("use_a", "helper", "CALLS", 2)]
    _create_graph_db(db, nodes, edges, with_method_cols=False)
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    block = patch_mod._graph_contract_block("lib/util.py")
    assert "[SIGNATURE] def helper(v)" in block, f"signature lost on legacy schema:\n{block}"
    assert "caller(s)" not in block, (
        f"BUG2: a caller COUNT was fabricated on a legacy schema (must abstain):\n{block}"
    )


# ===========================================================================
# BUG #3 — legacy-schema callee laundering: abstain when resolution_method absent
# ===========================================================================
def test_bug3_callee_contracts_abstain_on_legacy_schema(tmp_path, patch_mod):
    db = tmp_path / "legacy.db"
    nodes = [
        {"label": "Function", "name": "caller_fn", "file_path": "lib/a.py",
         "signature": "def caller_fn()"},
        {"label": "Function", "name": "callee_fn", "file_path": "lib/b.py",
         "signature": "def callee_fn(z)"},
    ]
    edges = [("caller_fn", "callee_fn", "CALLS", 2)]
    _create_graph_db(db, nodes, edges, with_method_cols=False)
    con = sqlite3.connect(str(db))
    try:
        out = patch_mod._edit_target_callee_contracts(con, "lib/a.py", ["caller_fn"])
        assert out == [], (
            f"BUG3: legacy schema (no resolution_method) laundered a callee as a "
            f"verified contract: {out}"
        )
    finally:
        con.close()


def test_bug3_callee_contracts_still_fire_on_deterministic_edge(tmp_path, patch_mod):
    """No over-suppression: a real deterministic callee still renders."""
    db = tmp_path / "graph.db"
    nodes = [
        {"label": "Function", "name": "caller_fn", "file_path": "lib/a.py",
         "signature": "def caller_fn()"},
        {"label": "Function", "name": "callee_fn", "file_path": "lib/b.py",
         "signature": "def callee_fn(z)"},
    ]
    edges = [("caller_fn", "callee_fn", "CALLS", 2, "import", 1.0)]
    _create_graph_db(db, nodes, edges)
    con = sqlite3.connect(str(db))
    try:
        out = patch_mod._edit_target_callee_contracts(con, "lib/a.py", ["caller_fn"])
        assert out and "callee_fn(z)" in out[0], f"deterministic callee lost: {out}"
    finally:
        con.close()


# ===========================================================================
# BUG #4 — sanitizers: hover-markdown signatures + unbalanced property values
# ===========================================================================
def test_bug4_sanitize_signature_strips_hover_markdown(patch_mod):
    raw = "```python\n(method) def wait(self, timeout: float) -> None\n```"
    assert patch_mod._sanitize_signature(raw) == "def wait(self, timeout: float) -> None"
    # fast path: already-clean signature untouched
    assert patch_mod._sanitize_signature("def f(x) -> int") == "def f(x) -> int"


def test_bug4_contract_block_signature_sanitized(tmp_path, patch_mod, monkeypatch):
    db = tmp_path / "graph.db"
    nodes = [
        {"label": "Method", "name": "wait", "file_path": "lib/w.py",
         "signature": "```python\n(method) def wait(self, timeout: float) -> None\n```"},
    ]
    _create_graph_db(db, nodes, [])
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    block = patch_mod._graph_contract_block("lib/w.py")
    assert "```" not in block and "(method)" not in block, (
        f"BUG4: raw pyright hover markdown rendered into <gt-contract>:\n{block}"
    )
    assert "def wait(self, timeout: float) -> None" in block


def test_bug4_callee_contract_signature_sanitized(tmp_path, patch_mod):
    db = tmp_path / "graph.db"
    nodes = [
        {"label": "Function", "name": "caller_fn", "file_path": "lib/a.py",
         "signature": "def caller_fn()"},
        {"label": "Function", "name": "callee_fn", "file_path": "lib/b.py",
         "signature": "```python\n(function) def callee_fn(z: int) -> str\n```"},
    ]
    edges = [("caller_fn", "callee_fn", "CALLS", 2, "import", 1.0)]
    _create_graph_db(db, nodes, edges)
    con = sqlite3.connect(str(db))
    try:
        out = patch_mod._edit_target_callee_contracts(con, "lib/a.py", ["caller_fn"])
        joined = "\n".join(out)
        assert "```" not in joined and "(function)" not in joined, (
            f"BUG4: hover markdown in callee contract:\n{joined}"
        )
        assert "def callee_fn(z: int) -> str" in joined
    finally:
        con.close()


def test_bug4_property_values_clipped_balanced(tmp_path, patch_mod, monkeypatch):
    """A guard_clause stored cut mid-expression (dangling operator) must be
    repaired by the balanced clip, never rendered raw."""
    db = tmp_path / "graph.db"
    nodes = [
        {"label": "Function", "name": "top_fn", "file_path": "lib/p.py",
         "signature": "def top_fn(x)", "key": "top_fn"},
    ]
    _create_graph_db(db, nodes, [])
    node_id = _create_graph_db.last_ids["top_fn"]  # type: ignore[attr-defined]
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO properties (node_id, kind, value, line) VALUES (?,?,?,?)",
                 (node_id, "guard_clause", "x > 0 and", 2))
    conn.execute("INSERT INTO properties (node_id, kind, value, line) VALUES (?,?,?,?)",
                 (node_id, "exception_flow",
                  'raise TypeError("unterminated literal', 3))
    conn.commit()
    conn.close()
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    block = patch_mod._graph_contract_block("lib/p.py")
    assert "x > 0 and" not in block, f"BUG4: dangling-operator guard rendered raw:\n{block}"
    assert '"unterminated literal' not in block, (
        f"BUG4: unterminated string literal rendered raw:\n{block}"
    )


# ===========================================================================
# BUG #5 — read-only URI open + one-time readability probe
# ===========================================================================
def test_bug5_connect_ro_works_on_readonly_file(tmp_path, patch_mod):
    db = tmp_path / "graph.db"
    _create_graph_db(db, [{"label": "Function", "name": "f", "file_path": "a.py",
                           "signature": "def f()"}], [])
    os.chmod(db, stat.S_IREAD)  # read-only file
    try:
        con = patch_mod._connect_ro(str(db))
        assert con is not None, "BUG5: _connect_ro failed on a read-only graph.db"
        try:
            row = con.execute("SELECT name FROM nodes LIMIT 1").fetchone()
            assert row == ("f",)
        finally:
            con.close()
    finally:
        os.chmod(db, stat.S_IWRITE | stat.S_IREAD)


def test_bug5_connect_ro_immutable_in_substrate_mode(tmp_path, patch_mod, monkeypatch):
    """On the truly-ro substrate mount the open uses immutable=1 (no WAL/locking)."""
    db = tmp_path / "graph.db"
    _create_graph_db(db, [{"label": "Function", "name": "f", "file_path": "a.py",
                           "signature": "def f()"}], [])
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    con = patch_mod._connect_ro(str(db))
    assert con is not None
    try:
        assert con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1
    finally:
        con.close()


def test_bug5_probe_prints_classified_line_once(tmp_path, patch_mod, capsys):
    """An unreadable graph prints ONE classified line, then stays quiet (no spam)."""
    bad = tmp_path / "bad.db"
    bad.write_text("this is not a sqlite database " * 30, encoding="utf-8")
    assert patch_mod._connect_ro(str(bad)) is None
    out1 = capsys.readouterr().out
    assert "[gt-patch] GRAPH_UNREADABLE_IN_CONTAINER:" in out1, (
        f"BUG5: unreadable graph did not print the classified probe line: {out1!r}"
    )
    # second use: quiet (correct-or-quiet, no per-turn spam)
    assert patch_mod._connect_ro(str(bad)) is None
    out2 = capsys.readouterr().out
    assert "GRAPH_UNREADABLE_IN_CONTAINER" not in out2, "BUG5: probe line spammed"


def test_bug5_all_pillars_route_through_connect_ro(two_init_repo, patch_mod, monkeypatch):
    """Every per-turn pillar must open the graph via _connect_ro (None -> quiet)."""
    _tmp, db = two_init_repo
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    monkeypatch.setattr(patch_mod, "_connect_ro", lambda _db: None)
    assert patch_mod._evidence_body("post_view", "pkg_a/__init__.py", str(_tmp)) == ""
    assert patch_mod._query_scope("pkg_a/__init__.py") == []
    assert patch_mod._consensus_block("pkg_b/__init__.py", str(_tmp)) == "" or \
        "graph-connected" not in patch_mod._consensus_block("pkg_b/__init__.py", str(_tmp))
    assert patch_mod._graph_contract_block("pkg_b/__init__.py") == ""
    assert patch_mod._cochange_block("pkg_b/__init__.py") == ""


# ===========================================================================
# BUG #6 — GT_BASELINE strict "1" parse (gt_agent + gt_mini_patch in lockstep)
# ===========================================================================
@pytest.mark.parametrize("value,expected", [("1", True), ("0", False),
                                            ("false", False), ("true", False)])
def test_bug6_gt_baseline_strict_parse(monkeypatch, value, expected):
    monkeypatch.setenv("GT_BASELINE", value)
    agent = _load(_AGENT_PATH, "gt_agent_dsf")
    patch = _load(_PATCH_PATH, "gt_mini_patch_dsf_b")
    assert agent._GT_BASELINE is expected, (
        f"BUG6: gt_agent GT_BASELINE={value!r} parsed as {agent._GT_BASELINE}"
    )
    assert patch._GT_BASELINE is expected, (
        f"BUG6: gt_mini_patch GT_BASELINE={value!r} parsed as {patch._GT_BASELINE}"
    )


# ===========================================================================
# BUG #7 — [GT_META] error line printed BEFORE every adapter raise; raise scope
#           consistent: (proof OR substrate) for witness AND brief.
# ===========================================================================
@pytest.fixture
def agent_mod(monkeypatch):
    _gt_env_clear(monkeypatch)
    return _load(_AGENT_PATH, "gt_agent_dsf_b7")


def test_bug7_brief_raise_prints_gt_meta_error_line(agent_mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))  # dir exists, brief.txt absent
    with pytest.raises(agent_mod.DeepSweAdapterError):
        agent_mod._generate_brief("fix the bug")
    out = capsys.readouterr().out
    assert "error=DEEPSWE_ADAPTER_FAIL" in out and "detail=" in out, (
        f"BUG7: adapter raised without the classified [GT_META] line: {out!r}"
    )


def test_bug7_brief_raise_prints_line_when_cert_dir_unset(agent_mod, monkeypatch, capsys):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    with pytest.raises(agent_mod.DeepSweAdapterError):
        agent_mod._substrate_brief()
    out = capsys.readouterr().out
    assert "error=DEEPSWE_ADAPTER_FAIL" in out


def test_bug7_brief_empty_raise_prints_line(agent_mod, monkeypatch, tmp_path, capsys):
    (tmp_path / "brief.txt").write_text("  \n ", encoding="utf-8")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    with pytest.raises(agent_mod.DeepSweAdapterError):
        agent_mod._substrate_brief()
    assert "error=DEEPSWE_ADAPTER_FAIL" in capsys.readouterr().out


def _make_edge_graph(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, signature TEXT, "
        "return_type TEXT, is_exported INT, is_test INT, language TEXT, parent_id INT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INT, "
        "target_id INT, type TEXT, source_line INT, source_file TEXT, "
        "resolution_method TEXT, confidence REAL, metadata TEXT)"
    )
    conn.execute("INSERT INTO nodes (label,name,file_path,language) "
                 "VALUES ('Function','A','a.py','python')")
    conn.execute("INSERT INTO nodes (label,name,file_path,language) "
                 "VALUES ('Function','B','b.py','python')")
    conn.execute("INSERT INTO edges (source_id,target_id,type,source_line,"
                 "resolution_method,confidence) VALUES (1,2,'CALLS',4,'import',1.0)")
    conn.commit()
    conn.close()


def test_bug7_witness_raises_on_mismatch_substrate_without_proof(
        agent_mod, monkeypatch, tmp_path, capsys):
    """Consistent raise scope: substrate active (GT_HOST_GRAPH_DB set) but NO
    GT_PROOF_MODE — a hash mismatch must STILL raise (DeepSweAdapterError
    docstring: 'under proof/substrate mode'), after printing the classified line."""
    db = tmp_path / "graph.db"
    _make_edge_graph(db)
    (tmp_path / "lsp_certificate.json").write_text(
        '{"graph_hash_after_lsp": "deadbeef_wrong"}', encoding="utf-8")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(db))
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("GT_LSP_CERT", str(tmp_path / "lsp_certificate.json"))
    with pytest.raises(agent_mod.DeepSweAdapterError, match="GRAPH_FAIL_HASH_MISMATCH"):
        agent_mod._emit_gt_meta_witness()
    out = capsys.readouterr().out
    assert "error=DEEPSWE_ADAPTER_FAIL" in out and "GRAPH_FAIL_HASH_MISMATCH" in out


def test_bug7_witness_warns_outside_proof_and_substrate(agent_mod, monkeypatch, capsys):
    """Outside BOTH proof and substrate the classified line still prints but no
    raise (dev/CI non-fatal) — e.g. a layer-1 import failure on the legacy path."""
    monkeypatch.setattr(agent_mod, "_proof_mode", lambda: False)
    monkeypatch.setattr(agent_mod, "_substrate_active", lambda: False)
    # Force the import-failed branch deterministically.
    import builtins
    real_import = builtins.__import__

    def _block(name, *a, **k):
        if name.startswith("groundtruth.runtime"):
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _block)
    agent_mod._emit_gt_meta_witness()  # must NOT raise
    out = capsys.readouterr().out
    assert "error=DEEPSWE_ADAPTER_FAIL" in out


# ===========================================================================
# BUG #8 — pier yaml: no blanket "verified facts" overclaim; tags explained
# ===========================================================================
def test_bug8_yaml_honest_and_explains_all_tags():
    import yaml
    text = _PIER_CFG.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)  # still valid yaml
    template = doc["agent"]["instance_template"]
    assert "they are verified cross-file facts" not in template, (
        "BUG8: blanket 'verified cross-file facts' overclaim still present"
    )
    for tag in ("<gt-contract>", "<gt-scope>", "<gt-cochange>", "<gt-nudge>"):
        assert tag in template, f"BUG8: missing one-line explanation for {tag}"
    assert "(unverified)" in template, (
        "BUG8: template must tell the agent unverified hints are labeled"
    )


# ===========================================================================
# BUG #9 — bare name_match caller hints carry "(unverified)"
# ===========================================================================
def test_bug9_unverified_tag_on_name_match_caller_hint(tmp_path, patch_mod):
    """target_fn has ONLY a name_match caller above the floor -> the [CALLERS]
    hint must be labeled (unverified), never a bare file:line."""
    db = tmp_path / "graph.db"
    root = tmp_path / "repo"
    root.mkdir()
    nodes = [
        {"label": "Function", "name": "target_fn", "file_path": "lib/t.py",
         "signature": "def target_fn()"},
        {"label": "Function", "name": "guess_fn", "file_path": "app/x.py"},
    ]
    edges = [("guess_fn", "target_fn", "CALLS", 12, "name_match", 0.9)]
    _create_graph_db(db, nodes, edges)
    con = sqlite3.connect(str(db))
    try:
        cc = patch_mod._caller_contract_for_file(con, "lib/t.py", str(root), ["target_fn"])
        assert cc, "expected an unverified hint for the floor-clearing name_match caller"
        assert "(unverified)" in cc, (
            f"BUG9: name_match caller hint rendered bare (no '(unverified)'): {cc!r}"
        )
    finally:
        con.close()
