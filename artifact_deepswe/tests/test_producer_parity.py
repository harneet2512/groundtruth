"""Byte-parity tests for the three DUPLICATED context producers.

GroundTruth ships each of three context families TWICE: once on the brief side
(``v1r_brief`` / ``hooks.post_view`` / ``pretask.curation_map``) and once on the
per-turn DeepSWE side (``artifact_deepswe.gt_mini_patch``). The two copies are supposed
to deliver the SAME fact set for a fixed ``graph.db``; where they DON'T, the agent sees
different "facts" depending on which surface fired. This module PINS that contract: for
one controlled temp graph it calls BOTH copies, normalizes each to a comparable set, and
asserts equality.

The three pairs (CLAUDE.md "ONE PRODUCT" rule — these must not fragment):

  COCHANGE   v1r_brief._co_change_from_table          vs  gt_mini_patch._cochange_block
  SCOPE      curation_map._neighbors (file 1-hop)     vs  gt_mini_patch._query_scope
  CONTRACT   post_view._contract_pillar               vs  gt_mini_patch._graph_contract_block

Cases that CURRENTLY DRIFT are marked ``xfail(strict=True)`` with a reason that names the
de-dup target. When a de-dup lands and the copies converge, the strict-xfail flips to an
unexpected PASS and the test goes RED — the tripwire that says "the drift you documented
is gone; delete the xfail." A drift test that silently passed would let the copies
re-diverge unnoticed; strict-xfail fails closed.

FACT-SET NOTE (commit bfe619e5, 2026-06-15): ``impl_method``/``unique_method`` were cut
from ``DETERMINISTIC_RESOLUTION_METHODS`` (receiver-unproven name-uniqueness rungs that
launder cross-receiver/external collisions). Any producer gated on the fact set must now
emit FEWER scope/caller facts. ``test_scope_impl_method_is_not_a_fact`` pins that both
copies treat an ``impl_method`` edge as a NON-fact.

DETERMINISM TRAP: ``post_view._contract_pillar`` loads issue anchors from a HARDCODED
``/tmp/gt_issue_anchors.json`` and SUPPRESSES the contract when no anchor matches the
file's functions. A stale file on the host makes the contract pillar non-deterministic
across machines. The ``anchored_env`` fixture pins that file so the contract comparison
is reproducible, then restores it (fail-closed: a parallel run's file is preserved).

The Rust ``[SIGNATURE] fd::walk::Batch`` bug (a Rust Method node whose stored
``signature`` is the impl-block module/class path, e.g. ``fd::dir_entry::DirEntry``, not
the ``fn name(...)`` header) is pinned by ``test_contract_rust_impl_class_signature``:
NEITHER producer has a structural "is this a callable header" guard, so both render the
garbage identically (verified on the live `fd` graph: a Method `path` carries
signature `fd::dir_entry::DirEntry`). That test documents the shared bug (xfail) so a fix
to EITHER copy without the other re-introduces drift.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# --- import the gt_mini_patch (per-turn) side ------------------------------------
_ARTIFACT_DIR = Path(__file__).resolve().parents[1]  # artifact_deepswe/
if str(_ARTIFACT_DIR) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_DIR))
import gt_mini_patch as mp  # noqa: E402

# --- import the brief side -------------------------------------------------------
groundtruth_available = True
_skip_reason = ""
try:
    from groundtruth.pretask import v1r_brief as brief  # noqa: E402
    from groundtruth.pretask import curation_map as cmap  # noqa: E402
    from groundtruth.hooks import post_view as pview  # noqa: E402
except Exception as _e:  # noqa: BLE001
    groundtruth_available = False
    _skip_reason = f"groundtruth package not importable: {_e}"

pytestmark = pytest.mark.skipif(
    not groundtruth_available, reason=_skip_reason or "groundtruth unavailable"
)

# The hardcoded path post_view._contract_pillar reads (the determinism trap).
_ANCHORS_PATH = "/tmp/gt_issue_anchors.json"


# ---------------------------------------------------------------------------------
# Temp graph.db builder. Deterministic, tiny, schema-faithful to what gt-index emits
# (the columns each producer SELECTs). Source file `widget.py` with two functions, a
# helper, a test file, a vendored file, an EXAMPLES file; CALLS edges of three
# provenance tiers (fact / non-fact impl_method / —); a cochanges table with a source,
# a test, and a doc partner; a Rust Method node carrying the impl-class-path sig bug.
# ---------------------------------------------------------------------------------
SRC = "src/widget.py"
SRC2 = "src/helper.py"
EXAMPLE = "examples/sample.py"
TEST = "tests/test_widget.py"
VENDOR = "vendor/jquery.min.js"
DOC = "CHANGELOG.md"
RUST = "src/dir_entry.rs"
RUST_BOGUS_SIG = "fd::dir_entry::DirEntry"


def _build_graph(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported INTEGER DEFAULT 0,
            is_test INTEGER DEFAULT 0, language TEXT NOT NULL, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
            type TEXT NOT NULL, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
        );
        CREATE TABLE cochanges (file_a TEXT, file_b TEXT, count INTEGER);
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER, kind TEXT,
            value TEXT, line INTEGER, confidence REAL DEFAULT 1.0
        );
        """
    )

    def node(label, name, fp, sig, lang="python", is_test=0, parent=None, ql=None):
        cur = con.execute(
            "INSERT INTO nodes(label,name,qualified_name,file_path,start_line,end_line,"
            "signature,return_type,is_exported,is_test,language,parent_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (label, name, ql or name, fp, 1, 9, sig, "", 1, is_test, lang, parent),
        )
        return cur.lastrowid

    n_apply = node("Function", "apply", SRC, "def apply(self, key, value):")
    n_render = node("Function", "render", SRC, "def render(self):")
    n_format = node("Function", "format_value", SRC2, "def format_value(v):")
    # an EXAMPLES-dir function reached from apply via a FACT edge: mini's
    # _is_test_or_demo_path drops `examples/`, _neighbors does not -> scope drift.
    n_example = node("Function", "demo_main", EXAMPLE, "def demo_main():")
    n_test = node("Function", "test_apply", TEST, "def test_apply():", is_test=1)
    node("Function", "jq", VENDOR, "function jq()", lang="javascript")
    # Rust Method whose stored signature is the impl-block module/class PATH (live `fd`
    # bug). label='Method' so it survives the Function/Method gate both producers use.
    node("Method", "path", RUST, RUST_BOGUS_SIG, lang="rust", ql="DirEntry.path")

    def edge(src, tgt, method, conf, sfile):
        con.execute(
            "INSERT INTO edges(source_id,target_id,type,source_line,source_file,"
            "resolution_method,confidence) VALUES(?,?,?,?,?,?,?)",
            (src, tgt, "CALLS", 5, sfile, method, conf),
        )

    edge(n_format, n_apply, "import", 1.0, SRC2)       # helper -> apply   (FACT)
    edge(n_test, n_apply, "import", 1.0, TEST)         # test -> apply     (FACT but is_test)
    edge(n_render, n_format, "impl_method", 0.6, SRC)  # render -> format  (NON-fact post bfe619e5)
    edge(n_apply, n_format, "import", 1.0, SRC)        # apply -> format   (callee contract material)
    edge(n_apply, n_example, "import", 1.0, SRC)       # apply -> demo_main (FACT, examples dir)

    con.execute("INSERT INTO cochanges VALUES(?,?,?)", (SRC, SRC2, 5))   # source partner (kept by both)
    con.execute("INSERT INTO cochanges VALUES(?,?,?)", (SRC, TEST, 4))   # test partner   (drift)
    con.execute("INSERT INTO cochanges VALUES(?,?,?)", (SRC, DOC, 9))    # doc partner    (drift)
    con.commit()
    con.close()


@pytest.fixture()
def anchored_env(tmp_path):
    """Pin /tmp/gt_issue_anchors.json so post_view._contract_pillar is deterministic,
    pointing every fixture function name at a matching anchor (so the pillar does NOT
    suppress). Backs up and restores any pre-existing file (fail-closed)."""
    had = os.path.exists(_ANCHORS_PATH)
    backup = open(_ANCHORS_PATH, encoding="utf-8").read() if had else None
    os.makedirs(os.path.dirname(os.path.abspath(_ANCHORS_PATH)), exist_ok=True)
    json.dump(
        {
            "symbols": ["apply", "render", "path"],
            "code_symbols": ["apply", "render", "path"],
            "title_symbols": [],
            "paths": [],
            "test_names": [],
        },
        open(_ANCHORS_PATH, "w", encoding="utf-8"),
    )
    try:
        yield
    finally:
        if backup is not None:
            open(_ANCHORS_PATH, "w", encoding="utf-8").write(backup)
        elif os.path.exists(_ANCHORS_PATH):
            os.unlink(_ANCHORS_PATH)


@pytest.fixture()
def graph_db(tmp_path) -> str:
    db = str(tmp_path / "graph.db")
    _build_graph(db)
    # Point the gt_mini_patch substrate-consume path at THIS graph; clear fire-once
    # latches + baseline so each producer actually runs. (GT_HOST_GRAPH_DB triggers a
    # fail-closed empty-handoff guard — harmless here, the nodes table is populated.)
    os.environ["GT_HOST_GRAPH_DB"] = db
    os.environ["GT_GRAPH_DB"] = db
    _reset_mini_latches()
    return db


def _reset_mini_latches() -> None:
    mp._GT_BASELINE = False
    mp._cochange_fired = False
    mp._contract_seen = set()
    mp._consensus_fired = False
    mp._seen = set()
    mp._consensus_scope = set()


# ---------------------------------------------------------------------------------
# Normalizers: collapse each producer's rendered text to the comparable SET.
# ---------------------------------------------------------------------------------
def _basename_set(paths) -> set[str]:
    out = set()
    for p in paths:
        p = (p or "").replace("\\", "/")
        out.add(p.split("/")[-1] if "/" in p else p)
    return out


def _cochange_partners_mini(rel: str) -> set[str]:
    _reset_mini_latches()
    block = mp._cochange_block(rel)
    out = set()
    for ln in block.splitlines():
        ln = ln.strip()
        if ln.startswith("- "):
            name = ln[2:].split(" (")[0].strip()
            out.add(name.split("/")[-1])
    return out


def _scope_files_mini(rel: str) -> set[str]:
    _reset_mini_latches()
    return _basename_set(mp._query_scope(rel))


def _contract_sigs_mini(rel: str) -> set[str]:
    """gt_mini `_graph_contract_block` -> set of signatures from [SIGNATURE] lines."""
    _reset_mini_latches()
    block = mp._graph_contract_block(rel)
    out = set()
    for ln in block.splitlines():
        ln = ln.strip()
        if ln.startswith("[SIGNATURE] "):
            out.add(ln[len("[SIGNATURE] "):].strip())
    return out


def _contract_sigs_brief(graph_db: str, rel: str) -> set[str]:
    """post_view `_contract_pillar` -> set of signatures (strip [CONTRACT]/trailing ret)."""
    con = sqlite3.connect(graph_db)
    try:
        lines = pview._contract_pillar(con, rel)
    finally:
        con.close()
    out = set()
    for ln in lines:
        ln = ln.strip()
        if ln.startswith("[CONTRACT] ") and not ln.startswith("[CONTRACT] flows:"):
            body = ln[len("[CONTRACT] "):].strip()
            if " -> " in body:
                body = body.rsplit(" -> ", 1)[0].strip()
            out.add(body)
    return out


def _file_node_ids(graph_db: str, rel: str) -> list[int]:
    con = sqlite3.connect(graph_db)
    try:
        rows = con.execute(
            "SELECT id FROM nodes WHERE file_path = ? AND label IN ('Function','Method')",
            (mp._norm_fp(rel),),
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _scope_files_brief(graph_db: str, rel: str) -> set[str]:
    """curation_map `_neighbors` (callers+callees of the focus file's symbols) -> file
    basenames — the closest brief-side analogue to gt_mini `_query_scope`'s file-1-hop
    set. Symbol-seeded, so it returns the neighbor symbols' files."""
    con = sqlite3.connect(graph_db)
    files: set[str] = set()
    try:
        ids = _file_node_ids(graph_db, rel)
        has_conf, has_method = mp._has_columns(con)
        for direction in ("callers", "callees"):
            edges = cmap._neighbors(
                con, ids, direction=direction, has_conf=has_conf,
                has_method=has_method, max_neighbors=6, repo_root="",
            )
            for e in edges:
                f = getattr(e, "file", "") or ""
                if f and mp._norm_fp(f) != mp._norm_fp(rel):
                    files.add(f.replace("\\", "/").split("/")[-1])
    finally:
        con.close()
    return files


# =================================================================================
# PARITY TEST 1 — COCHANGE
# =================================================================================
def test_cochange_shared_source_partner(graph_db):
    """Shared correct behavior (NOT xfail): the real SOURCE co-change partner
    (helper.py) is delivered by BOTH copies. If either drops it, completeness broke."""
    mini = _cochange_partners_mini(SRC)
    brief_partners = _basename_set(brief._co_change_from_table(graph_db, SRC, limit=8))
    assert "helper.py" in mini, f"mini dropped source partner: {sorted(mini)}"
    assert "helper.py" in brief_partners, f"brief dropped source partner: {sorted(brief_partners)}"


@pytest.mark.xfail(
    strict=True,
    reason="DE-DUP TARGET (cochange) — BIDIRECTIONAL drift on the SAME cochanges table:\n"
    "  * gt_mini_patch._cochange_block drops TEST/demo partners "
    "(_is_test_or_demo_path) and vendored partners, but KEEPS doc/config partners "
    "(no .md/.rst/.yml filter) -> emits CHANGELOG.md.\n"
    "  * v1r_brief._co_change_from_table drops doc/config partners IN SQL "
    "(.md/.rst/.txt/.yml/.yaml) but KEEPS test partners (no test filter) "
    "-> emits tests/test_widget.py.\n"
    "Neither is a superset; they share only helper.py. Single-source ONE partner "
    "filter (drop test + demo + vendored + doc/config) so both surfaces match.",
)
def test_cochange_partner_set_parity(graph_db):
    mini = _cochange_partners_mini(SRC)
    brief_partners = _basename_set(brief._co_change_from_table(graph_db, SRC, limit=8))
    assert mini == brief_partners, (
        f"cochange drift: mini={sorted(mini)} brief={sorted(brief_partners)}"
    )


# =================================================================================
# PARITY TEST 2 — SCOPE
# =================================================================================
def test_scope_shared_fact_neighbor(graph_db):
    """Shared correct behavior (NOT xfail): the FACT neighbor (helper.py, reached via an
    `import` edge into apply()) is in BOTH scope copies."""
    mini = _scope_files_mini(SRC)
    brief_scope = _scope_files_brief(graph_db, SRC)
    assert "helper.py" in mini, f"mini scope missing fact neighbor: {sorted(mini)}"
    assert "helper.py" in brief_scope, f"brief scope missing fact neighbor: {sorted(brief_scope)}"


def test_scope_impl_method_is_not_a_fact(graph_db):
    """FACT-SET CUT pin (bfe619e5): impl_method is NO LONGER a fact. BOTH the gt_mini
    scope fact-clause and the curation_map fact set must exclude it — otherwise the
    laundered cross-receiver collisions (httpx.post->fastapi.post, Stdout::lock->
    Batch.lock) re-enter scope as 'graph-connected'."""
    con = sqlite3.connect(graph_db)
    try:
        clause = mp._scope_fact_clause(con)
    finally:
        con.close()
    assert "impl_method" not in clause, (
        "gt_mini scope fact-clause re-admitted impl_method (bfe619e5 regression)"
    )
    assert "impl_method" not in {m.lower() for m in cmap.DETERMINISTIC_RESOLUTION_METHODS}, (
        "curation_map fact set re-admitted impl_method (bfe619e5 regression)"
    )
    assert "unique_method" not in {m.lower() for m in cmap.DETERMINISTIC_RESOLUTION_METHODS}, (
        "curation_map fact set re-admitted unique_method (bfe619e5 regression)"
    )


def test_scope_file_set_parity(graph_db):
    """DE-DUP LANDED (scope) — both copies now exclude vendored + test/demo neighbour
    FILES via the SAME canonical seam (delivery.path_policy.is_test_or_demo /
    is_vendored_path): gt_mini_patch._query_scope:2172-2174 and
    curation_map._neighbors (the file-level filter added there). On this fixture
    apply() -> demo_main is a FACT edge into examples/sample.py — previously the
    brief surfaced it and the twin dropped it (delivery drift); now BOTH drop it.
    This is now a fail-closed parity tripwire: if either copy stops using the seam,
    the demo file re-enters one scope set and this assertion bites."""
    mini = _scope_files_mini(SRC)
    brief_scope = _scope_files_brief(graph_db, SRC)
    assert mini == brief_scope, f"scope drift: mini={sorted(mini)} brief={sorted(brief_scope)}"


# =================================================================================
# PARITY TEST 3 — CONTRACT / SIGNATURE
# =================================================================================
def test_contract_python_signature_set_parity(graph_db, anchored_env):
    """Shared correct behavior (NOT xfail): for a clean Python file, the [SIGNATURE]
    set (mini) and the [CONTRACT] signature set (brief) MATCH — both read nodes.signature
    for the file's Function/Method rows. Requires anchored_env (the pillar suppresses
    without a matching issue anchor; gt_mini always fires). The two surfaces converge on
    the same signatures here; they DIVERGE on block shape (mini adds [CALLERS]/PRESERVE,
    brief ranks by anchor) — pinned separately below."""
    mini = _contract_sigs_mini(SRC)
    brief_sigs = _contract_sigs_brief(graph_db, SRC)
    assert mini == brief_sigs, f"contract sig drift: mini={sorted(mini)} brief={sorted(brief_sigs)}"


@pytest.mark.xfail(
    strict=True,
    reason="DE-DUP TARGET (contract block shape): even when the signature SETS match, "
    "the two blocks differ — gt_mini_patch._graph_contract_block adds a [CALLERS] line + "
    "a PRESERVE/[RAISES]/[RETURNS] property family and ranks by caller count with a "
    "builtin-shadow drop; post_view._contract_pillar ranks by issue-anchor relevance with "
    "a no-anchor SUPPRESSION gate and a data_flow 'flows:' family. Same source signatures, "
    "two different renderers + two different gates. Single-source the per-file fetch, the "
    "ranking, the suppression gate, and the property family.",
)
def test_contract_block_shape_parity(graph_db, anchored_env):
    _reset_mini_latches()
    mini_block = mp._graph_contract_block(SRC)
    con = sqlite3.connect(graph_db)
    try:
        brief_lines = pview._contract_pillar(con, SRC)
    finally:
        con.close()
    mini_tags = {ln.split(" ")[0] for ln in mini_block.splitlines() if ln.strip().startswith("[")}
    brief_tags = {ln.split(" ")[0] for ln in brief_lines if ln.strip().startswith("[")}
    assert mini_tags == brief_tags, (
        f"contract block-shape drift: mini_tags={sorted(mini_tags)} brief_tags={sorted(brief_tags)}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="SHARED RUST BUG ([SIGNATURE] fd::walk::Batch): a Rust Method node whose stored "
    "nodes.signature is the impl-block module/class PATH (fd::dir_entry::DirEntry), not the "
    "`fn name(...)` header. NEITHER producer has a structural 'is this a callable header' "
    "guard, so BOTH render the garbage as a [SIGNATURE]/[CONTRACT] fact (verified on the "
    "live `fd` graph). This asserts the FIXED behavior (neither emits it); it xfails today "
    "and flips RED the moment a guard lands — forcing the SAME guard into both copies. The "
    "guard belongs in the indexer (don't store a non-header as a function signature) AND/OR "
    "a shared sanitizer reject (a signature with no '(' and no fn/def keyword is not a fact).",
)
def test_contract_rust_impl_class_signature(graph_db, anchored_env):
    mini = _contract_sigs_mini(RUST)
    brief_sigs = _contract_sigs_brief(graph_db, RUST)
    assert RUST_BOGUS_SIG not in mini, f"gt_mini emitted impl-class path as signature: {sorted(mini)}"
    assert RUST_BOGUS_SIG not in brief_sigs, f"brief emitted impl-class path as signature: {sorted(brief_sigs)}"


def test_contract_rust_bug_is_present_in_both_today(graph_db, anchored_env):
    """Sanity/negative control (NOT xfail): documents that the Rust sig bug is CURRENTLY
    emitted by BOTH copies. This is the inverse of the xfail above — it asserts today's
    (broken) reality, so it stays GREEN until a guard lands, at which point BOTH this test
    and the xfail flip. If only ONE copy stops emitting the garbage, THIS test fails first
    and names which copy drifted ahead of the other."""
    mini = _contract_sigs_mini(RUST)
    brief_sigs = _contract_sigs_brief(graph_db, RUST)
    assert RUST_BOGUS_SIG in mini, "gt_mini no longer emits the Rust garbage (fix the xfail)"
    assert RUST_BOGUS_SIG in brief_sigs, "brief no longer emits the Rust garbage (fix the xfail)"
