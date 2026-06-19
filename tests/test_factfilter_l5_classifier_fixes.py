"""PATH B per-layer health audit fixes — red->green tests (run 27260307167).

Two confirmed defect families from
`.claude/reports/runs/pathB_verified_trial_27260307167/PER_LAYER_HEALTH.md`,
fixed at the DELIVERY/fact-filter surface (never the rank/anchor/fusion paths):

  DEFECT 1a — vendored/minified/generated paths delivered as FACTS:
     `astropy/extern/jquery/.../jquery.dataTables.min.js:9` rendered as a
     resolved [WITNESS]/caller fact on astropy-13236. Fix: a generalized
     path-class filter (`/extern/`, `/vendor/`, `/third_party/`,
     `/node_modules/`, `/dist/`, `/_generated/`, `*.min.js`, protobuf/codegen
     markers) + a minified-content heuristic (mean line length > 200) applied
     to every DELIVERED fact surface in gt_mini_patch.py (witnesses, callers,
     callee contracts, consensus scope, co-change, per-edit contract) and to
     v1r_brief's witness/caller fact filters + the "Related files to inspect"
     render. Extends the localizer's `_is_generated` W_GEN demote (ranking)
     to the delivery surface, per the audit. A vendored edge is never a
     deterministic FACT (gt_gt §2.3 trust model).

  DEFECT 1b — builtin/dunder-shadow laundering:
     `isinstance -> TableColumns.isinstance` delivered as
     `[CALLERS] isinstance: 1048 verified caller(s) ... preserve this
     interface` on astropy-13236/13033/13453. A bare builtin call resolves
     verified_unique (0.95, deterministic) when ONE project symbol shadows the
     builtin name — the resolver's T2 builtin drop (gt_gt §2.3) covers
     QUALIFIED calls only. Fix: builtin/dunder callable names (mirroring
     resolver.go builtinMethodNames/strongBuiltinMethodNames + the shadowable
     Python/JS/Go/Rust builtins) are excluded from CERTIFIED/[CALLERS]/
     [WITNESS]/[CALLEE]/contract delivery. Same family as the §2.5
     stdlib-shadow guard (commit 55ab30eb) — this closes the bare-call
     residual at the consumer.

  DEFECT 2 — L5 `failure_persisted` fired on ENVIRONMENT errors (5/7 false
     positives; 1 firing reinforced reverting a gold-equivalent edit on
     django-10097). Fix in gt_mini_patch._l5_failure_nudge: (gate 1) only a
     real TEST-RUNNER invocation can falsify a hypothesis (a scratch script /
     stale visible fixture cannot); (gate 2) env/tooling failure markers
     (pip / ModuleNotFoundError / build / network / version-shim) -> SILENT;
     (gate 3) an explicit test/assertion-failure marker is REQUIRED.
     Correct-or-quiet: uncertain -> silent. The `loop` nudge (1/1 false) now
     requires the same command to produce the SAME observation (no new state).

  Cross-layer — the `[gt-patch:loaded]` loader banner leaked into
     agent-visible stdout on 10/10 tasks. Moved to stderr (loader telemetry,
     not agent content).

All deterministic: sqlite fixtures, no network, no task IDs, no gold labels.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"

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
    return _load(_PATCH_PATH, "gt_mini_patch_ff")


# ---------------------------------------------------------------------------
# graph.db fixture builder (Go-indexer output schema)
# ---------------------------------------------------------------------------
def _create_graph_db(db_path: Path, nodes: list[dict], edges: list[tuple],
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
    conn.execute(
        "CREATE TABLE properties (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "node_id INTEGER, kind TEXT, value TEXT, line INTEGER)"
    )
    if cochanges is not None:
        conn.execute("CREATE TABLE cochanges (file_a TEXT, file_b TEXT, count INTEGER)")
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
        key_to_id[n.get("key", n["name"])] = conn.execute(
            "SELECT last_insert_rowid()").fetchone()[0]
    for src, tgt, etype, line, method, conf in edges:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, source_line, "
            "resolution_method, confidence) VALUES (?,?,?,?,?,?)",
            (key_to_id[src], key_to_id[tgt], etype, line, method, conf),
        )
    conn.commit()
    conn.close()


_VENDORED_JS = "astropy/extern/jquery/data/js/jquery.dataTables.min.js"


@pytest.fixture
def pollution_repo(tmp_path: Path):
    """The astropy-13236 pollution shape, generalized:
      - table.py: real funcs `validate_rows` (1 clean deterministic caller from
        core.py — a TRUE fact that must SURVIVE) + builtin-shadow methods
        `isinstance` and `__init__` with deterministic callers (the launder).
      - a vendored minified JS file with a deterministic edge INTO table.py
        (the jquery [WITNESS]/caller pollution) and one FROM table.py.
      - a node_modules neighbor + a vendored co-change partner.
    """
    db = tmp_path / "graph.db"
    repo = tmp_path / "src"
    (repo / "astropy" / "table").mkdir(parents=True)
    (repo / "astropy" / "core").mkdir(parents=True)
    (repo / "astropy" / "extern" / "jquery" / "data" / "js").mkdir(parents=True)
    (repo / "astropy" / "table" / "table.py").write_text(
        "class TableColumns:\n"
        "    def isinstance(self, cls):\n"
        "        return list(cls)\n"
        "    def __init__(self):\n"
        "        pass\n"
        "    def validate_rows(self, data):\n"
        "        return helper_check(data)\n",
        encoding="utf-8",
    )
    (repo / "astropy" / "core" / "core.py").write_text(
        "def run_pipeline(t):\n"
        "    return t.validate_rows([1])\n"
        "def use_iso(t):\n"
        "    return t.isinstance(dict)\n"
        "def make_one():\n"
        "    return TableColumns()\n",
        encoding="utf-8",
    )
    # minified vendored file: one very long line
    (repo / _VENDORED_JS).write_text(
        "var gb=function(a){return a};" * 200 + "\n", encoding="utf-8",
    )
    nodes = [
        {"label": "Method", "name": "isinstance", "key": "iso",
         "file_path": "astropy/table/table.py",
         "signature": "def isinstance(self, cls)", "start_line": 2, "end_line": 3},
        {"label": "Method", "name": "__init__", "key": "init",
         "file_path": "astropy/table/table.py",
         "signature": "def __init__(self)", "start_line": 4, "end_line": 5},
        {"label": "Method", "name": "validate_rows", "key": "validate_rows",
         "file_path": "astropy/table/table.py",
         "signature": "def validate_rows(self, data)", "start_line": 6, "end_line": 7},
        {"label": "Function", "name": "run_pipeline", "key": "run_pipeline",
         "file_path": "astropy/core/core.py",
         "signature": "def run_pipeline(t)", "start_line": 1, "end_line": 2},
        {"label": "Function", "name": "use_iso", "key": "use_iso",
         "file_path": "astropy/core/core.py",
         "signature": "def use_iso(t)", "start_line": 3, "end_line": 4},
        {"label": "Function", "name": "make_one", "key": "make_one",
         "file_path": "astropy/core/core.py",
         "signature": "def make_one()", "start_line": 5, "end_line": 6},
        {"label": "Function", "name": "gb", "key": "gb",
         "file_path": _VENDORED_JS,
         "signature": "function gb(a)", "start_line": 1, "end_line": 1,
         "language": "javascript"},
        {"label": "Function", "name": "helper_check", "key": "helper_check",
         "file_path": "node_modules/checker/index.js",
         "signature": "function helper_check(d)", "start_line": 1, "end_line": 1,
         "language": "javascript"},
    ]
    edges = [
        # TRUE fact: core.py calls validate_rows (deterministic, must survive)
        ("run_pipeline", "validate_rows", "CALLS", 2, "import", 1.0),
        # builtin-shadow launder: deterministic-tagged callers of isinstance/__init__
        ("use_iso", "iso", "CALLS", 4, "verified_unique", 0.95),
        ("make_one", "init", "CALLS", 6, "verified_unique", 0.95),
        # vendored pollution: minified JS calls INTO table.py + is called FROM it
        ("gb", "validate_rows", "CALLS", 1, "import", 1.0),
        ("validate_rows", "gb", "CALLS", 7, "import", 1.0),
        # node_modules callee of validate_rows
        ("validate_rows", "helper_check", "CALLS", 7, "import", 1.0),
    ]
    _create_graph_db(
        db, nodes, edges,
        cochanges=[("astropy/table/table.py", _VENDORED_JS, 9),
                   ("astropy/table/table.py", "astropy/core/core.py", 4)],
    )
    return repo, db


# ===========================================================================
# DEFECT 1a — vendored/minified paths are never DELIVERED facts
# ===========================================================================
def test_vendored_path_classifier(patch_mod):
    is_v = patch_mod._is_vendored_path
    assert is_v(_VENDORED_JS)
    assert is_v("vendor/golang.org/x/net/http2.go")
    assert is_v("third_party/lib/foo.c")
    assert is_v("web/node_modules/lodash/index.js")
    assert is_v("pkg/dist/bundle.js")
    assert is_v("app/static/app.min.css")
    assert is_v("api/zz_generated.deepcopy.go")
    assert is_v("proto/service_pb2.py")
    # real source files never excluded
    assert not is_v("astropy/table/table.py")
    assert not is_v("src/distribute.py")  # 'dist' must match as a path segment only
    assert not is_v("internal/extern_api.go")


def test_vendored_caller_never_a_witness(pollution_repo, patch_mod):
    repo, db = pollution_repo
    con = sqlite3.connect(str(db))
    try:
        wits = patch_mod._resolved_witnesses_for_file(
            con, "astropy/table/table.py", str(repo), max_each=4)
    finally:
        con.close()
    rendered = " ".join(f"{w['file_path']} {w['symbol']} {w['target']}" for w in wits)
    assert "extern" not in rendered, f"vendored witness leaked: {rendered}"
    assert "node_modules" not in rendered, f"node_modules witness leaked: {rendered}"
    # the TRUE deterministic caller fact survives
    assert any(w["file_path"] == "astropy/core/core.py" and w["target"] == "validate_rows"
               for w in wits), f"true caller fact was over-suppressed: {wits}"


def test_vendored_file_view_emits_no_evidence(pollution_repo, patch_mod, monkeypatch):
    repo, db = pollution_repo
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    body = patch_mod._evidence_body("post_view", _VENDORED_JS, str(repo))
    assert body == "", f"evidence delivered FOR a vendored/minified file: {body!r}"


def test_minified_content_heuristic_excludes_unlisted_bundle(tmp_path, patch_mod):
    """A minified file OUTSIDE any vendor dir is caught by the content heuristic."""
    repo = tmp_path / "src"
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "bundle.js").write_text("x=1;" * 1200 + "\n", encoding="utf-8")
    (repo / "web" / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    assert patch_mod._is_delivery_excluded("web/bundle.js", str(repo))
    assert not patch_mod._is_delivery_excluded("web/app.py", str(repo))


def test_consensus_scope_excludes_vendored(pollution_repo, patch_mod, monkeypatch):
    repo, db = pollution_repo
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    scope = patch_mod._query_scope("astropy/table/table.py")
    joined = " ".join(scope)
    assert "extern" not in joined and "node_modules" not in joined, scope
    root_file = tmp = repo.parent / "gt_root.txt"
    tmp.write_text(str(repo), encoding="utf-8")
    monkeypatch.setenv("GT_ROOT_FILE", str(root_file))
    block = patch_mod._consensus_block("astropy/table/table.py", str(repo))
    assert "extern" not in block and "node_modules" not in block, block


def test_cochange_excludes_vendored_partner(pollution_repo, patch_mod, monkeypatch):
    repo, db = pollution_repo
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    block = patch_mod._cochange_block("astropy/table/table.py")
    assert "jquery" not in block and "extern" not in block, block
    assert "core.py" in block  # the real co-change partner survives


# ===========================================================================
# DEFECT 1b — builtin/dunder-shadow names are never CALLERS/contract facts
# ===========================================================================
def test_builtin_shadow_name_classifier(patch_mod):
    is_b = patch_mod._is_builtin_shadow_name
    for n in ("isinstance", "len", "get", "join", "items", "split", "loads",
              "append", "exists", "__init__", "__call__", "Isinstance"):
        assert is_b(n), n
    for n in ("validate_rows", "set_fields", "URLValidator", "get_order_by", ""):
        assert not is_b(n), n


def test_builtin_shadow_never_a_contract_fact(pollution_repo, patch_mod, monkeypatch):
    repo, db = pollution_repo
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    block = patch_mod._graph_contract_block("astropy/table/table.py")
    assert "isinstance" not in block, f"builtin-shadow laundered as contract: {block}"
    assert "__init__" not in block, f"dunder laundered as contract: {block}"
    # the real method still renders with its true verified caller count
    assert "validate_rows" in block, f"true contract over-suppressed: {block}"
    assert "[CALLERS] validate_rows: 1 verified caller(s) in 1 file(s)" in block


def test_builtin_shadow_excluded_from_top_func_names(pollution_repo, patch_mod):
    repo, db = pollution_repo
    con = sqlite3.connect(str(db))
    try:
        names = patch_mod._top_func_names(con, "astropy/table/table.py", limit=5)
    finally:
        con.close()
    assert "isinstance" not in names and "__init__" not in names, names
    assert "validate_rows" in names


def test_builtin_shadow_never_a_witness(pollution_repo, patch_mod):
    repo, db = pollution_repo
    con = sqlite3.connect(str(db))
    try:
        wits = patch_mod._resolved_witnesses_for_file(
            con, "astropy/table/table.py", str(repo), max_each=4)
    finally:
        con.close()
    targets = {w["target"] for w in wits} | {w["symbol"] for w in wits}
    assert "isinstance" not in targets and "__init__" not in targets, wits


# ===========================================================================
# DEFECT 2 — L5 failure_persisted: env-vs-hypothesis classification
# ===========================================================================
_ENV_OUT = (
    "Traceback (most recent call last):\n"
    '  File "/testbed/test_x.py", line 1, in <module>\n'
    "    import erfa\n"
    "ModuleNotFoundError: No module named 'erfa'\n"
)
_REAL_FAIL_OUT = (
    "============================= FAILURES =============================\n"
    "FAILED astropy/table/tests/test_table.py::test_columns - AssertionError: "
    "assert [1] == [2]\n"
    "1 failed, 3 passed in 0.21s\n"
)


def test_failure_nudge_silent_on_env_error(patch_mod):
    """5/7 PATH B firings were pip/import/build env errors — must be SILENT."""
    patch_mod._source_edit_count = 1
    for _ in range(3):
        out = patch_mod._l5_failure_nudge("python -m pytest astropy/", _ENV_OUT)
    assert out == "", f"failure_persisted fired on an ENVIRONMENT error: {out!r}"


def test_failure_nudge_silent_on_scratch_script(patch_mod):
    """django-10097 harm: a scratch check against a stale fixture is NOT a
    real-test falsification — never contradict an edit not yet validated
    against the real test."""
    patch_mod._source_edit_count = 1
    scratch_out = "AssertionError: URL should be invalid\n"
    for _ in range(3):
        out = patch_mod._l5_failure_nudge("python /tmp/check_urls.py", scratch_out)
    assert out == "", f"failure_persisted fired on a scratch script: {out!r}"


def test_failure_nudge_fires_on_persistent_real_test_failure(patch_mod):
    patch_mod._source_edit_count = 1
    out1 = patch_mod._l5_failure_nudge("python -m pytest astropy/table/tests/", _REAL_FAIL_OUT)
    assert out1 == ""  # first observation: not yet persisted
    out2 = patch_mod._l5_failure_nudge("python -m pytest astropy/table/tests/", _REAL_FAIL_OUT)
    assert 'reason="failure_persisted"' in out2, "real persistent test failure must fire"


def test_failure_nudge_runtests_style_runner_fires(patch_mod):
    """django runtests.py (the one CORRECT PATH B firing, 10554) still fires."""
    patch_mod._source_edit_count = 1
    out = "FAIL: test_aggregate (ordering.tests.OrderingTests)\nAssertionError: x\nFAILED (failures=1)\n"
    patch_mod._l5_failure_nudge("./tests/runtests.py ordering", out)
    out2 = patch_mod._l5_failure_nudge("./tests/runtests.py ordering", out)
    assert 'reason="failure_persisted"' in out2


def test_failure_nudge_env_marker_wins_over_fail_marker(patch_mod):
    """Mixed output (a FAILED line + an env error) is UNCERTAIN -> silent."""
    patch_mod._source_edit_count = 1
    mixed = _REAL_FAIL_OUT + "\nImportError while importing test module 'x'\n"
    for _ in range(3):
        out = patch_mod._l5_failure_nudge("python -m pytest astropy/", mixed)
    assert out == ""


# ===========================================================================
# DEFECT 2 — loop nudge requires identical observation (no new state)
# ===========================================================================
def test_loop_nudge_silent_when_outputs_differ(patch_mod):
    """13453 false fire: same command, NEW state each run -> not a loop."""
    for i in range(6):
        out = patch_mod._l5_nudge("python runner.py", f"progress step {i}")
    assert out == "", f"loop nudge fired despite changing observations: {out!r}"


def test_loop_nudge_fires_on_identical_command_and_output(patch_mod):
    out = ""
    for _ in range(5):
        out = out or patch_mod._l5_nudge("python runner.py", "same error, no change")
    assert 'reason="loop"' in out


# ===========================================================================
# Cross-layer — [gt-patch:loaded] banner goes to stderr, never agent stdout
# ===========================================================================
def test_gt_patch_banner_on_stderr_not_agent_output(patch_mod, capsys):
    out = {"output": "file contents", "returncode": 0}
    patch_mod._augment_output({"command": "cat README.md"}, out)
    assert "[gt-patch:loaded]" not in out["output"], (
        "loader banner leaked into agent-visible output")
    assert "[gt-patch:loaded]" in capsys.readouterr().err


# ===========================================================================
# v1r_brief (L1) — same fact filters at the brief's witness/caller surface
# ===========================================================================
@pytest.fixture
def v1r_mod():
    sys.path.insert(0, str(_ROOT / "src"))
    import groundtruth.pretask.v1r_brief as vb
    return vb


def test_v1r_witnesses_exclude_vendored_and_builtin(pollution_repo, v1r_mod):
    repo, db = pollution_repo
    wits = v1r_mod._resolved_witnesses_for_file(
        str(db), "astropy/table/table.py", str(repo), max_each=4)
    rendered = " ".join(f"{w['file_path']} {w['symbol']} {w['target']}" for w in wits)
    assert "extern" not in rendered and "node_modules" not in rendered, rendered
    targets = {w["target"] for w in wits} | {w["symbol"] for w in wits}
    assert "isinstance" not in targets and "__init__" not in targets, wits
    assert any(w["target"] == "validate_rows" and w["file_path"] == "astropy/core/core.py"
               for w in wits), f"true fact over-suppressed: {wits}"


def test_v1r_caller_contract_excludes_vendored_and_builtin(pollution_repo, v1r_mod):
    repo, db = pollution_repo
    line = v1r_mod._caller_contract_for_file(
        str(db), "astropy/table/table.py", str(repo),
        ["isinstance", "validate_rows"])
    assert "isinstance" not in line.replace("use_iso", ""), line
    assert "extern" not in line and "node_modules" not in line, line
    assert "run_pipeline() in astropy/core/core.py" in line  # true fact survives
