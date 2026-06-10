"""v1r HOST brief cross-language CALLS-edge disqualifier — red->green tests.

The per-turn mini delivery (artifact_deepswe/gt_mini_patch.py) already carries
the cross-language disqualifier (_LANG_FAMILIES / _lang_family /
_is_cross_language_pair, applied at every delivered-fact site — DeepSWE
non-Python audit, run 27290157847, boa ledger [57]). The v1r HOST pre-task
brief (src/groundtruth/pretask/v1r_brief.py) had NONE of it, so on a
mixed-language repo (boa: rust+js) the host brief still laundered
cross-language deterministic-STAMPED edges as facts: a JS caller cited for a
Rust file at the [CALLERS] (_caller_contract_for_file), [WITNESS]
(_resolved_witnesses_for_file) and [CALLEE] (edit_target_callee_contracts)
surfaces.

These tests PORT the mini fixture shape (tests/test_minipatch_nonpython_audit_
fixes.py::crosslang_repo) against the HOST functions:

  - a .js caller with a deterministic-stamped (impl_method) CALLS edge to a
    .rs target must NEVER render as a caller fact / caller witness;
  - a .js callee of a .rs function (verified_unique stamp) must NEVER render
    as a callee witness / callee contract;
  - TRUE same-language facts must SURVIVE (no over-suppression);
  - SAME-FAMILY pairs (js->ts) must SURVIVE (one compilation unit);
  - a legacy graph WITHOUT nodes.language stays PERMISSIVE (the suppression
    itself must be a fact — unknown language is never judged) and never
    crashes.

All deterministic: sqlite fixtures, no network, no task IDs, no gold labels.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask.contract_map import edit_target_callee_contracts
from groundtruth.pretask.v1r_brief import (
    _caller_contract_for_file,
    _resolved_witnesses_for_file,
)

_RS_SOURCE = "core/engine/src/module/source.rs"
_RS_CALLER = "core/engine/src/vm/caller.rs"
_RS_PARSER = "core/engine/src/parser.rs"
_JS_BENCH = "benches/scripts/v8-benches/deltablue.js"
_JS_TOOL = "tools/fmt.js"
_TS_LIB = "lib/util.ts"
_JS_LIB = "lib/helper.js"


def _create_graph_db(db_path: Path, nodes, edges, *, with_language: bool = True) -> None:
    conn = sqlite3.connect(db_path)
    lang_col = "language TEXT," if with_language else ""
    conn.execute(
        f"""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
            name TEXT NOT NULL, qualified_name TEXT, file_path TEXT NOT NULL,
            start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported BOOLEAN DEFAULT 0,
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
    key_to_id: dict[str, int] = {}
    for n in nodes:
        if with_language:
            conn.execute(
                "INSERT INTO nodes (label, name, file_path, signature, start_line, "
                "end_line, is_test, language) VALUES (?,?,?,?,?,?,?,?)",
                (n["label"], n["name"], n["file_path"], n.get("signature", ""),
                 n.get("start_line", 1), n.get("end_line", 1), int(n.get("is_test", 0)),
                 n.get("language", "python")),
            )
        else:
            conn.execute(
                "INSERT INTO nodes (label, name, file_path, signature, start_line, "
                "end_line, is_test) VALUES (?,?,?,?,?,?,?)",
                (n["label"], n["name"], n["file_path"], n.get("signature", ""),
                 n.get("start_line", 1), n.get("end_line", 1), int(n.get("is_test", 0))),
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


def _write_repo_files(repo: Path) -> None:
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
        "pub fn run_loop(ctx: &mut Context) {\n"
        "    let m = module();\n"
        "    m.execute(ctx);\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / _RS_PARSER).write_text(
        "// parser\npub fn parse_module(src: &str) {\n}\n", encoding="utf-8"
    )
    (repo / _JS_BENCH).write_text(
        "function chainTest(n) {\n    plan.execute();\n}\n", encoding="utf-8"
    )
    (repo / _JS_TOOL).write_text(
        "function format_output(p) {\n    return p;\n}\n", encoding="utf-8"
    )
    (repo / _TS_LIB).write_text(
        "export function startServer(port: number) {\n}\n", encoding="utf-8"
    )
    (repo / _JS_LIB).write_text(
        "function boot() {\n    startServer(80);\n}\n", encoding="utf-8"
    )


_NODES = [
    {"label": "Method", "name": "execute", "key": "execute",
     "file_path": _RS_SOURCE, "signature": "pub fn execute(&self, ctx: &mut Context)",
     "start_line": 2, "end_line": 4, "language": "rust"},
    {"label": "Function", "name": "load_module", "key": "load_module",
     "file_path": _RS_SOURCE, "signature": "pub fn load_module(path: &str)",
     "start_line": 6, "end_line": 9, "language": "rust"},
    {"label": "Function", "name": "run_loop", "key": "run_loop",
     "file_path": _RS_CALLER, "signature": "pub fn run_loop(ctx: &mut Context)",
     "start_line": 1, "end_line": 4, "language": "rust"},
    {"label": "Function", "name": "parse_module", "key": "parse_module",
     "file_path": _RS_PARSER, "signature": "pub fn parse_module(src: &str)",
     "start_line": 2, "end_line": 3, "language": "rust"},
    {"label": "Function", "name": "chainTest", "key": "chainTest",
     "file_path": _JS_BENCH, "signature": "function chainTest(n)",
     "start_line": 1, "end_line": 3, "language": "javascript"},
    {"label": "Function", "name": "format_output", "key": "format_output",
     "file_path": _JS_TOOL, "signature": "function format_output(p)",
     "start_line": 1, "end_line": 3, "language": "javascript"},
    {"label": "Function", "name": "startServer", "key": "startServer",
     "file_path": _TS_LIB, "signature": "export function startServer(port: number)",
     "start_line": 1, "end_line": 2, "language": "typescript"},
    {"label": "Function", "name": "boot", "key": "boot",
     "file_path": _JS_LIB, "signature": "function boot()",
     "start_line": 1, "end_line": 3, "language": "javascript"},
]

_EDGES = [
    # TRUE same-language fact (must survive): caller.rs run_loop -> execute
    ("run_loop", "execute", "CALLS", 3, "impl_method", 0.9),
    # CROSS-LANGUAGE pollution (boa [57]): js chainTest -> rust execute, det stamp
    ("chainTest", "execute", "CALLS", 2, "impl_method", 0.6),
    # Rust load_module -> rust parse_module (true callee, survives)
    ("load_module", "parse_module", "CALLS", 7, "same_file", 1.0),
    # Rust load_module -> JS format_output (cross-language callee, det stamp)
    ("load_module", "format_output", "CALLS", 8, "verified_unique", 0.95),
    # SAME-family js -> ts (must survive)
    ("boot", "startServer", "CALLS", 2, "import", 1.0),
]


@pytest.fixture
def crosslang_repo(tmp_path: Path):
    db = tmp_path / "graph.db"
    repo = tmp_path / "src"
    _write_repo_files(repo)
    _create_graph_db(db, _NODES, _EDGES)
    return repo, db


# ===========================================================================
# [CALLERS] surface — _caller_contract_for_file (v1r HOST)
# ===========================================================================
def test_host_cross_language_caller_never_a_caller_fact(crosslang_repo):
    repo, db = crosslang_repo
    line = _caller_contract_for_file(str(db), _RS_SOURCE, str(repo), ["execute"])
    assert "deltablue" not in line, f"cross-language caller fact leaked: {line}"
    assert "run_loop() in " + _RS_CALLER in line, f"true caller lost: {line}"


def test_host_cross_language_caller_not_even_an_unverified_hint(tmp_path):
    """A cross-language edge is impossible — it must not degrade to an
    '(unverified)' location hint either; it is DROPPED."""
    db = tmp_path / "graph.db"
    repo = tmp_path / "src"
    _write_repo_files(repo)
    # Only the cross-language caller, but stamped name_match above the floor:
    # without the disqualifier it renders as an (unverified) hint.
    edges = [("chainTest", "execute", "CALLS", 2, "name_match", 0.9)]
    _create_graph_db(db, _NODES, edges)
    line = _caller_contract_for_file(str(db), _RS_SOURCE, str(repo), ["execute"])
    assert "deltablue" not in line, f"cross-language hint leaked: {line}"


# ===========================================================================
# [WITNESS] surface — _resolved_witnesses_for_file (v1r HOST)
# ===========================================================================
def test_host_cross_language_caller_never_a_witness(crosslang_repo):
    repo, db = crosslang_repo
    wits = _resolved_witnesses_for_file(str(db), _RS_SOURCE, str(repo), max_each=4)
    rendered = " ".join(f"{w['file_path']} {w['symbol']} {w['target']}" for w in wits)
    assert "deltablue" not in rendered, f"cross-language caller witness leaked: {rendered}"
    assert "fmt.js" not in rendered, f"cross-language callee witness leaked: {rendered}"
    # TRUE same-language facts survive
    assert any(w["direction"] == "caller" and w["file_path"] == _RS_CALLER
               for w in wits), f"true rust caller over-suppressed: {wits}"
    assert any(w["direction"] == "callee" and w["file_path"] == _RS_PARSER
               for w in wits), f"true rust callee over-suppressed: {wits}"


def test_host_same_family_js_ts_witness_survives(crosslang_repo):
    repo, db = crosslang_repo
    wits = _resolved_witnesses_for_file(str(db), _TS_LIB, str(repo), max_each=4)
    assert any(w["file_path"] == _JS_LIB for w in wits), (
        f"same-family js->ts caller wrongly suppressed: {wits}")


# ===========================================================================
# [CALLEE] surface — edit_target_callee_contracts (contract_map, rendered by
# v1r_brief._edit_target_contracts_block)
# ===========================================================================
def test_host_cross_language_callee_never_a_callee_contract(crosslang_repo):
    repo, db = crosslang_repo
    out = edit_target_callee_contracts(str(db), _RS_SOURCE, ["load_module"])
    names = [cc.callee for cc in out]
    assert "format_output" not in names, f"cross-language callee contract leaked: {names}"
    assert "parse_module" in names, f"true callee contract lost: {names}"


# ===========================================================================
# Legacy schema (no nodes.language) — PERMISSIVE, never crash
# ===========================================================================
def test_host_legacy_schema_without_language_is_permissive(tmp_path):
    db = tmp_path / "graph.db"
    repo = tmp_path / "src"
    _write_repo_files(repo)
    _create_graph_db(db, _NODES, _EDGES, with_language=False)
    # No language column -> cannot judge -> every deterministic edge still a fact.
    line = _caller_contract_for_file(str(db), _RS_SOURCE, str(repo), ["execute"])
    assert "run_loop() in " + _RS_CALLER in line, f"legacy fact lost: {line}"
    assert "deltablue" in line, (
        "legacy schema must stay permissive (cannot judge language): " + line)
    wits = _resolved_witnesses_for_file(str(db), _RS_SOURCE, str(repo), max_each=4)
    assert any(w["file_path"] == _RS_CALLER for w in wits), f"legacy witness lost: {wits}"
    out = edit_target_callee_contracts(str(db), _RS_SOURCE, ["load_module"])
    assert "parse_module" in [cc.callee for cc in out], "legacy callee contract lost"
