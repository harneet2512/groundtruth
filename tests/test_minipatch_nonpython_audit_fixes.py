"""DeepSWE non-Python trajectory audit fixes — red->green tests (run 27290157847).

Three confirmed defect families from the 4-language §4 ledgers
(`task_ledgers/{abs-module-cache-flags, csstree-shorthand-expansion-compression,
arktype-json-schema-refs-dependencies, boa-hierarchical-evaluation-cancellation}.md`)
fixed at the DELIVERY surface in gt_mini_patch.py (never the rank/anchor/fusion paths):

  DEFECT A — CROSS-LANGUAGE edges delivered as deterministic FACTS:
     boa ledger [57]: `chainTest() in benches/scripts/v8-benches/deltablue.js:1112
     'plan.execute();'` rendered as a [CALLERS] fact for Rust
     `core/engine/src/module/source.rs`. Measured on the run's own graph.db:
     214 cross-language CALLS edges, of which 99 carry DETERMINISTIC stamps
     (verified_unique=42, impl_method=26, type_flow=24, unique_method=7) — the
     indexer's typed tiers match candidates ACROSS languages, so the
     _DETERMINISTIC_METHODS fact gate admits them and the vendored-path filter
     misses them (benches/ is first-party). A source-level call edge between
     files of DIFFERENT language families is impossible (tree-sitter call
     resolution is intra-language); same-toolchain families (js/ts, java/kotlin/
     scala, c/c++/objc/swift) are kept compatible. Unknown/legacy-schema
     languages stay PERMISSIVE (never suppress what we cannot judge).

  DEFECT B — STALE line-keyed witnesses after agent edits (L6 OFF by design on
     the substrate): boa [109] quotes the agent's OWN just-inserted doc comment
     as `root_shape calls -> context/mod.rs:545 '/// Returns an error...'`;
     [257] quotes a bare `}`; arktype json.ts:92 snippet stale after rewrite.
     Fix: SNIPPET ATTESTATION — a witness/caller-fact row whose live code line
     does not mention the symbol the graph attributes to it has drifted and is
     suppressed entirely (correct-or-quiet: the line number is no longer a
     fact). An unreadable file (empty snippet) is not drift evidence.

  DEFECT C — missing governor: submit-with-zero-observed-test-results.
     boa [243]-[333]: six `timeout N cargo test` runs SIGKILLed mid-build, the
     agent saw only compile lines, concluded ([332]) "tests seem to pass but
     it's not printing results", submitted, reward 0 (the grader's build was
     even corrupted by the kills). The submit action never reaches
     env.execute() (mini-swe intercepts it: `<exception>action was not
     executed</exception>`), so the governor fires on the PATTERN: a real
     test-runner invocation (the existing _TEST_RUNNER_RE gate) whose output
     carries NO test result (no pass marker, no fail marker), no env failure,
     and no compile error — a BLIND run. Two blind runs + >=1 source edit +
     zero test evidence all session -> ONE nudge. Pass/fail/env/compile
     output keeps it silent (correct-or-quiet).

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
    return _load(_PATCH_PATH, "gt_mini_patch_npa")


# ---------------------------------------------------------------------------
# graph.db fixture builder (Go-indexer output schema, language column included)
# ---------------------------------------------------------------------------
def _create_graph_db(
    db_path: Path, nodes: list[dict], edges: list[tuple], with_language: bool = True
) -> None:
    conn = sqlite3.connect(str(db_path))
    lang_col = "language TEXT NOT NULL DEFAULT 'python'," if with_language else ""
    conn.execute(
        f"""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, name TEXT NOT NULL,
            qualified_name TEXT, file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, {lang_col} parent_id INTEGER
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
    key_to_id: dict[str, int] = {}
    for n in nodes:
        if with_language:
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
        else:
            conn.execute(
                "INSERT INTO nodes (label, name, file_path, signature, start_line, "
                "end_line, is_test) VALUES (?,?,?,?,?,?,?)",
                (
                    n["label"],
                    n["name"],
                    n["file_path"],
                    n.get("signature", ""),
                    n.get("start_line", 1),
                    n.get("end_line", 1),
                    int(n.get("is_test", 0)),
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


_RS_SOURCE = "core/engine/src/module/source.rs"
_RS_CALLER = "core/engine/src/vm/caller.rs"
_RS_PARSER = "core/engine/src/parser.rs"
_JS_BENCH = "benches/scripts/v8-benches/deltablue.js"
_JS_TOOL = "tools/fmt.js"
_TS_LIB = "lib/util.ts"
_JS_LIB = "lib/helper.js"


@pytest.fixture
def crosslang_repo(tmp_path: Path):
    """The boa [57] pollution shape, generalized:
    - source.rs: Rust method `execute` + fn `load_module`.
    - caller.rs: Rust fn `run_loop` calls execute (impl_method, det) — a TRUE
      same-language fact that must SURVIVE (snippet mentions `execute`).
    - deltablue.js (NOT under any vendor dir): javascript fn `chainTest` with a
      deterministic-STAMPED (impl_method 0.6) CALLS edge to the Rust `execute`
      — the cross-language pollution that must be suppressed.
    - fmt.js: javascript callee of the Rust `load_module` (det stamp) — must
      never render as a [CALLEE] contract.
    - util.ts / helper.js: a js-family caller of a ts-family target — SAME
      family, must survive.
    """
    db = tmp_path / "graph.db"
    repo = tmp_path / "src"
    for rel in (_RS_SOURCE, _RS_CALLER, _RS_PARSER, _JS_BENCH, _JS_TOOL, _TS_LIB, _JS_LIB):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / _RS_SOURCE).write_text(
        "impl SourceTextModule {\n"
        "    pub fn execute(&self, ctx: &mut Context) -> JsResult<()> {\n"
        "        Ok(())\n"
        "    }\n"
        "}\n"
        "pub fn load_module(path: &str) {\n"
        "    parse_module(path);\n"
        "    format_output(path);\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / _RS_CALLER).write_text(
        "pub fn run_loop(ctx: &mut Context) {\n    let m = module();\n    m.execute(ctx);\n}\n",
        encoding="utf-8",
    )
    (repo / _RS_PARSER).write_text(
        "// parser\npub fn parse_module(src: &str) {\n}\n",
        encoding="utf-8",
    )
    (repo / _JS_BENCH).write_text(
        "function chainTest(n) {\n    plan.execute();\n}\n",
        encoding="utf-8",
    )
    (repo / _JS_TOOL).write_text(
        "function format_output(p) {\n    return p;\n}\n",
        encoding="utf-8",
    )
    (repo / _TS_LIB).write_text(
        "export function startServer(port: number) {\n}\n",
        encoding="utf-8",
    )
    (repo / _JS_LIB).write_text(
        "function boot() {\n    startServer(80);\n}\n",
        encoding="utf-8",
    )
    nodes = [
        {
            "label": "Method",
            "name": "execute",
            "key": "execute",
            "file_path": _RS_SOURCE,
            "signature": "pub fn execute(&self, ctx: &mut Context)",
            "start_line": 2,
            "end_line": 4,
            "language": "rust",
        },
        {
            "label": "Function",
            "name": "load_module",
            "key": "load_module",
            "file_path": _RS_SOURCE,
            "signature": "pub fn load_module(path: &str)",
            "start_line": 6,
            "end_line": 9,
            "language": "rust",
        },
        {
            "label": "Function",
            "name": "run_loop",
            "key": "run_loop",
            "file_path": _RS_CALLER,
            "signature": "pub fn run_loop(ctx: &mut Context)",
            "start_line": 1,
            "end_line": 4,
            "language": "rust",
        },
        {
            "label": "Function",
            "name": "parse_module",
            "key": "parse_module",
            "file_path": _RS_PARSER,
            "signature": "pub fn parse_module(src: &str)",
            "start_line": 2,
            "end_line": 3,
            "language": "rust",
        },
        {
            "label": "Function",
            "name": "chainTest",
            "key": "chainTest",
            "file_path": _JS_BENCH,
            "signature": "function chainTest(n)",
            "start_line": 1,
            "end_line": 3,
            "language": "javascript",
        },
        {
            "label": "Function",
            "name": "format_output",
            "key": "format_output",
            "file_path": _JS_TOOL,
            "signature": "function format_output(p)",
            "start_line": 1,
            "end_line": 3,
            "language": "javascript",
        },
        {
            "label": "Function",
            "name": "startServer",
            "key": "startServer",
            "file_path": _TS_LIB,
            "signature": "export function startServer(port: number)",
            "start_line": 1,
            "end_line": 2,
            "language": "typescript",
        },
        {
            "label": "Function",
            "name": "boot",
            "key": "boot",
            "file_path": _JS_LIB,
            "signature": "function boot()",
            "start_line": 1,
            "end_line": 3,
            "language": "javascript",
        },
    ]
    edges = [
        # TRUE same-language fact (must survive): caller.rs run_loop -> execute
        # (type_flow = receiver-PROVEN; impl_method removed from the fact set 2026-06-15)
        ("run_loop", "execute", "CALLS", 3, "type_flow", 0.9),
        # CROSS-LANGUAGE pollution (boa [57]): js chainTest -> rust execute, det stamp
        ("chainTest", "execute", "CALLS", 2, "type_flow", 0.6),
        # Rust load_module -> rust parse_module (true callee, survives)
        ("load_module", "parse_module", "CALLS", 7, "same_file", 1.0),
        # Rust load_module -> JS format_output (cross-language callee, det stamp)
        ("load_module", "format_output", "CALLS", 8, "verified_unique", 0.95),
        # SAME-family js -> ts (must survive)
        ("boot", "startServer", "CALLS", 2, "import", 1.0),
    ]
    _create_graph_db(db, nodes, edges)
    return repo, db


# ===========================================================================
# DEFECT A — cross-language edges are never DELIVERED facts
# ===========================================================================
def test_lang_family_classifier(patch_mod):
    fam = patch_mod._lang_family
    # same-toolchain families stay compatible
    assert fam("javascript") == fam("typescript")
    assert fam("java") == fam("kotlin")
    assert fam("c") == fam("cpp")
    # distinct languages are distinct families
    assert fam("rust") != fam("javascript")
    assert fam("go") != fam("python")
    # unknown / empty -> None (permissive downstream)
    assert fam("") is None
    assert fam(None) is None
    assert fam("brainfuck") is None


def test_cross_language_pair_classifier(patch_mod):
    x = patch_mod._is_cross_language_pair
    assert x("javascript", "rust")
    assert x("python", "go")
    assert not x("javascript", "typescript")
    assert not x("java", "kotlin")
    assert not x("rust", "rust")
    # unknown on either side -> permissive (never suppress what we cannot judge)
    assert not x("", "rust")
    assert not x("klingon", "rust")
    assert not x(None, None)


def test_cross_language_caller_never_a_witness(crosslang_repo, patch_mod):
    repo, db = crosslang_repo
    con = sqlite3.connect(str(db))
    try:
        wits = patch_mod._resolved_witnesses_for_file(con, _RS_SOURCE, str(repo), max_each=4)
    finally:
        con.close()
    rendered = " ".join(f"{w['file_path']} {w['symbol']} {w['target']}" for w in wits)
    assert "deltablue" not in rendered, f"cross-language caller leaked: {rendered}"
    assert "fmt.js" not in rendered, f"cross-language callee leaked: {rendered}"
    # TRUE same-language facts survive
    assert any(w["direction"] == "caller" and w["file_path"] == _RS_CALLER for w in wits), (
        f"true rust caller over-suppressed: {wits}"
    )
    assert any(w["direction"] == "callee" and w["file_path"] == _RS_PARSER for w in wits), (
        f"true rust callee over-suppressed: {wits}"
    )


def test_cross_language_caller_never_a_caller_fact(crosslang_repo, patch_mod):
    repo, db = crosslang_repo
    con = sqlite3.connect(str(db))
    try:
        line = patch_mod._caller_contract_for_file(con, _RS_SOURCE, str(repo), ["execute"])
    finally:
        con.close()
    assert "deltablue" not in line, f"cross-language caller fact leaked: {line}"
    assert "run_loop() in " + _RS_CALLER in line, f"true caller lost: {line}"


def test_cross_language_callee_never_a_callee_contract(crosslang_repo, patch_mod):
    repo, db = crosslang_repo
    con = sqlite3.connect(str(db))
    try:
        out = patch_mod._edit_target_callee_contracts(con, _RS_SOURCE, ["load_module"])
    finally:
        con.close()
    joined = " ".join(out)
    assert "format_output" not in joined, f"cross-language callee contract leaked: {joined}"
    assert "parse_module" in joined, f"true callee contract lost: {joined}"


def test_same_family_js_ts_witness_survives(crosslang_repo, patch_mod):
    repo, db = crosslang_repo
    con = sqlite3.connect(str(db))
    try:
        wits = patch_mod._resolved_witnesses_for_file(con, _TS_LIB, str(repo), max_each=4)
    finally:
        con.close()
    assert any(w["file_path"] == _JS_LIB for w in wits), (
        f"same-family js->ts caller wrongly suppressed: {wits}"
    )


def test_contract_caller_count_excludes_cross_language(crosslang_repo, patch_mod, monkeypatch):
    repo, db = crosslang_repo
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    block = patch_mod._graph_contract_block(_RS_SOURCE)
    # execute has 2 det-stamped callers (rust run_loop + js chainTest);
    # only the same-language one is a deliverable count
    assert "[CALLERS] execute: 1 verified caller(s) in 1 file(s)" in block, block


def test_legacy_schema_without_language_is_permissive(tmp_path, patch_mod):
    """A graph without nodes.language cannot be judged -> no suppression, no crash."""
    db = tmp_path / "graph.db"
    repo = tmp_path / "src"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def callee():\n    pass\n", encoding="utf-8")
    (repo / "pkg" / "b.py").write_text("def caller():\n    callee()\n", encoding="utf-8")
    nodes = [
        {
            "label": "Function",
            "name": "callee",
            "key": "callee",
            "file_path": "pkg/a.py",
            "signature": "def callee()",
            "start_line": 1,
            "end_line": 2,
        },
        {
            "label": "Function",
            "name": "caller",
            "key": "caller",
            "file_path": "pkg/b.py",
            "signature": "def caller()",
            "start_line": 1,
            "end_line": 2,
        },
    ]
    edges = [("caller", "callee", "CALLS", 2, "import", 1.0)]
    _create_graph_db(db, nodes, edges, with_language=False)
    con = sqlite3.connect(str(db))
    try:
        wits = patch_mod._resolved_witnesses_for_file(con, "pkg/a.py", str(repo), max_each=4)
    finally:
        con.close()
    assert any(w["file_path"] == "pkg/b.py" for w in wits), (
        f"legacy schema wrongly suppressed: {wits}"
    )


# ===========================================================================
# DEFECT B — stale line-keyed witnesses are suppressed (snippet attestation)
# ===========================================================================
@pytest.fixture
def stale_repo(tmp_path: Path):
    """boa [109]/[257] shape: the graph's line numbers no longer match the live
    file (the agent edited it; L6 reindex OFF by design on the substrate).
      - ctx/mod.rs: graph says callee `root_shape` is defined at line 2, but the
        live line 2 is the agent's own doc comment.
      - cache.rs: graph says fn `update_cache` calls load_module at line 2, but
        the live line 2 is a bare `}`.
      - parser.rs: fresh definition (line content matches) — must survive.
    """
    db = tmp_path / "graph.db"
    repo = tmp_path / "src"
    for rel in ("ctx/mod.rs", "cache.rs", "parser.rs", "main.rs"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / "ctx" / "mod.rs").write_text(
        "pub struct Context {}\n"
        "/// Returns an error if the handle is already cancelled.\n"
        "pub fn something_else() {}\n",
        encoding="utf-8",
    )
    (repo / "cache.rs").write_text(
        "pub fn update_cache() {\n}\n// trailing\n",
        encoding="utf-8",
    )
    (repo / "parser.rs").write_text(
        "// parser\npub fn parse_module(src: &str) {\n}\n",
        encoding="utf-8",
    )
    (repo / "main.rs").write_text(
        "pub fn load_module(path: &str) {\n    root_shape();\n    parse_module(path);\n}\n",
        encoding="utf-8",
    )
    nodes = [
        {
            "label": "Function",
            "name": "load_module",
            "key": "load_module",
            "file_path": "main.rs",
            "signature": "pub fn load_module(path: &str)",
            "start_line": 1,
            "end_line": 4,
            "language": "rust",
        },
        # STALE: graph thinks root_shape is defined at ctx/mod.rs:2 (live: doc comment)
        {
            "label": "Function",
            "name": "root_shape",
            "key": "root_shape",
            "file_path": "ctx/mod.rs",
            "signature": "pub fn root_shape()",
            "start_line": 2,
            "end_line": 2,
            "language": "rust",
        },
        # STALE caller: graph thinks update_cache's call site is cache.rs:2 (live: `}`)
        {
            "label": "Function",
            "name": "update_cache",
            "key": "update_cache",
            "file_path": "cache.rs",
            "signature": "pub fn update_cache()",
            "start_line": 1,
            "end_line": 2,
            "language": "rust",
        },
        # FRESH callee
        {
            "label": "Function",
            "name": "parse_module",
            "key": "parse_module",
            "file_path": "parser.rs",
            "signature": "pub fn parse_module(src: &str)",
            "start_line": 2,
            "end_line": 3,
            "language": "rust",
        },
    ]
    edges = [
        # callee witness: load_module -> root_shape (def line stale)
        ("load_module", "root_shape", "CALLS", 2, "same_file", 1.0),
        # caller witness: update_cache -> load_module at cache.rs:2 (stale call site)
        ("update_cache", "load_module", "CALLS", 2, "import", 1.0),
        # fresh callee: load_module -> parse_module (parser.rs:2 contains the name)
        ("load_module", "parse_module", "CALLS", 3, "import", 1.0),
    ]
    _create_graph_db(db, nodes, edges)
    return repo, db


def test_snippet_attestation_helper(patch_mod):
    att = patch_mod._snippet_attests
    assert att("plan.execute();", "execute")
    assert att("pub fn parse_module(src: &str) {", "parse_module")
    assert not att("/// Returns an error if the handle is already cancelled.", "root_shape")
    assert not att("}", "enter_realm")
    # empty snippet (file unreadable) is NOT drift evidence
    assert att("", "anything")
    assert att("code line", "")


def test_stale_callee_witness_suppressed(stale_repo, patch_mod):
    repo, db = stale_repo
    con = sqlite3.connect(str(db))
    try:
        wits = patch_mod._resolved_witnesses_for_file(con, "main.rs", str(repo), max_each=4)
    finally:
        con.close()
    stale = [w for w in wits if w["symbol"] == "root_shape"]
    assert not stale, f"stale callee witness rendered (quotes a drifted doc-comment line): {stale}"
    # the FRESH callee witness survives
    assert any(w["symbol"] == "parse_module" for w in wits), (
        f"fresh callee witness over-suppressed: {wits}"
    )


def test_stale_caller_witness_suppressed(stale_repo, patch_mod):
    repo, db = stale_repo
    con = sqlite3.connect(str(db))
    try:
        wits = patch_mod._resolved_witnesses_for_file(con, "main.rs", str(repo), max_each=4)
    finally:
        con.close()
    stale = [w for w in wits if w["direction"] == "caller" and w["file_path"] == "cache.rs"]
    assert not stale, f"stale caller witness rendered (call-site line is a bare brace): {stale}"


def test_stale_caller_fact_suppressed_in_caller_contract(stale_repo, patch_mod):
    repo, db = stale_repo
    con = sqlite3.connect(str(db))
    try:
        line = patch_mod._caller_contract_for_file(con, "main.rs", str(repo), ["load_module"])
    finally:
        con.close()
    assert "update_cache()" not in line, f"drifted call-site line rendered as a caller FACT: {line}"


# ===========================================================================
# DEFECT C — no_test_evidence governor (submit-with-zero-observed-test-results)
# ===========================================================================
_CARGO_BLIND = (
    "   Compiling boa_engine v1.0.0-dev (/app/core/engine)\n"
    "    Checking boa_cli v1.0.0-dev (/app/cli)\n"
)
_CARGO_PASS = "test result: ok. 412 passed; 0 failed; 0 ignored\n"
_CARGO_COMPILE_ERR = (
    "error[E0596]: cannot borrow `context` as mutable\n"
    "error: could not compile `boa_engine` (lib test) due to 1 previous error\n"
)
_GO_FAIL = "--- FAIL: TestRequire (0.01s)\nFAIL\nexit status 1\n"
_NPM_PASS = "  16725 passing (40s)\n"


def test_no_test_evidence_fires_after_two_blind_test_runs(patch_mod):
    patch_mod._source_edit_count = 1
    out1 = patch_mod._l5_no_test_evidence_nudge(
        "timeout 60 cargo test --lib 2>&1 | tail -20", _CARGO_BLIND
    )
    assert out1 == ""  # first blind run: not yet a pattern
    out2 = patch_mod._l5_no_test_evidence_nudge(
        "timeout 120 cargo test evaluation 2>&1 | tail -30", _CARGO_BLIND
    )
    assert 'reason="no_test_evidence"' in out2, (
        "two blind test runs after a source edit must fire the governor"
    )


def test_no_test_evidence_fires_once(patch_mod):
    patch_mod._source_edit_count = 1
    patch_mod._l5_no_test_evidence_nudge("cargo test", _CARGO_BLIND)
    out2 = patch_mod._l5_no_test_evidence_nudge("cargo test", _CARGO_BLIND)
    assert out2 != ""
    out3 = patch_mod._l5_no_test_evidence_nudge("cargo test", _CARGO_BLIND)
    assert out3 == "", "governor must fire at most once"


def test_no_test_evidence_silent_when_pass_seen(patch_mod):
    patch_mod._source_edit_count = 1
    assert patch_mod._l5_no_test_evidence_nudge("npm test", _NPM_PASS) == ""
    for _ in range(4):
        out = patch_mod._l5_no_test_evidence_nudge("cargo test", _CARGO_BLIND)
    assert out == "", "test evidence was observed this session -> stay silent"


def test_no_test_evidence_silent_when_fail_seen(patch_mod):
    patch_mod._source_edit_count = 1
    assert patch_mod._l5_no_test_evidence_nudge("go test -run TestRequire ./...", _GO_FAIL) == ""
    for _ in range(4):
        out = patch_mod._l5_no_test_evidence_nudge("cargo test", _CARGO_BLIND)
    assert out == "", "a real FAIL is test evidence -> blind-run pattern is moot"


def test_no_test_evidence_silent_on_compile_error(patch_mod):
    """A compile error is actionable feedback, not blindness."""
    patch_mod._source_edit_count = 1
    for _ in range(4):
        out = patch_mod._l5_no_test_evidence_nudge("cargo test", _CARGO_COMPILE_ERR)
    assert out == ""


def test_no_test_evidence_silent_without_source_edit(patch_mod):
    patch_mod._source_edit_count = 0
    for _ in range(4):
        out = patch_mod._l5_no_test_evidence_nudge("cargo test", _CARGO_BLIND)
    assert out == ""


def test_no_test_evidence_silent_on_non_test_command(patch_mod):
    patch_mod._source_edit_count = 1
    for _ in range(4):
        out = patch_mod._l5_no_test_evidence_nudge("cargo check", "Checking ...\n")
    assert out == "", "cargo check is not a test-runner invocation"


def test_no_test_evidence_wired_into_augment_output(patch_mod, tmp_path, monkeypatch):
    """End-to-end: two blind cargo test commands through _augment_output deliver
    the nudge into the agent-visible output."""
    patch_mod._source_edit_count = 1
    out1 = {"output": _CARGO_BLIND, "returncode": 0}
    patch_mod._augment_output({"command": "timeout 60 cargo test | tail -5"}, out1)
    out2 = {"output": _CARGO_BLIND, "returncode": 0}
    patch_mod._augment_output({"command": "timeout 120 cargo test | tail -5"}, out2)
    assert 'reason="no_test_evidence"' in out2["output"], out2["output"]
