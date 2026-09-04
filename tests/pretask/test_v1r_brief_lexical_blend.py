"""Lexical-false-positive blend — the brief must LEAD with the structurally
central function / the gold file, never a function whose name coincidentally
matches a natural-language word in the issue text.

Three root causes pinned here (all language-agnostic, recur on rust/py/js):

  A. ``_top_function_names`` / ``_top_functions`` sorted ``CASE WHEN name IN
     (anchors) THEN 0`` — an ABSOLUTE hoist of any NL-word match above degree, so
     ``span.rs::start`` (ref=1, matched NL "start code point") beat ``new``
     (ref=32). The fix: hoist on CODE-SYMBOL anchors (backtick provenance) only;
     an NL-WORD anchor is a tiebreak under ``ref_count DESC``, never above it.

  B. ``_with_graph_map`` tried only ``function_names[0]`` per file, so when the
     #1 localizer file's chosen focus abstained, the lead silently ceded to a
     lower-ranked file. The fix: the #1 file gets a fallback list of focus
     functions, keeping its lead.

  C. Vendored / demo copies (``benchmark/libs/...``, ``examples/...``) were
     scored as candidates. The fix: a single fail-closed demote guard before the
     brief's entries are built, reusing ``path_policy.is_test_or_demo`` +
     ``is_vendored_path`` (no new ad-hoc list).

Mutation-check note: reverting the provenance blend in ``_top_function_names``
back to the absolute ``THEN 0`` hoist makes ``test_nl_word_does_not_beat_degree``
RED (the ref=1 word-coincidence leads), proving the assertion bites.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from unittest.mock import patch, MagicMock

from groundtruth.pretask.v1r_brief import (
    FileEntry,
    _top_functions,
    _top_function_names,
    render_brief,
    generate_v1r_brief,
)
from groundtruth.delivery.path_policy import is_test_or_demo, is_vendored_path


_BLEND_SCHEMA = """
    CREATE TABLE nodes (
        id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
        file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
        return_type TEXT, is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
        language TEXT DEFAULT 'python', parent_id INTEGER
    );
    CREATE TABLE edges (
        id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
        source_line INTEGER, source_file TEXT, resolution_method TEXT,
        confidence REAL DEFAULT 1.0, metadata TEXT
    );
"""


def _build(
    tmp_path: Path,
    nodes: list[tuple[int, str, str, str]],
    edges: list[tuple[int, int]],
) -> str:
    """nodes = (id, label, name, file_path); edges = (source_id, target_id).

    Every edge is a verified ``import`` CALL @ conf 1.0 so the confidence floor
    never strips the ref-count we are pinning the order against.
    """
    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(_BLEND_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id, label, name, file_path, signature) VALUES (?,?,?,?,?)",
        [(i, lbl, nm, fp, f"def {nm}()") for (i, lbl, nm, fp) in nodes],
    )
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
        "VALUES (?,?,'CALLS','import',1.0)",
        edges,
    )
    conn.commit()
    conn.close()
    return db


# --- ROOT CAUSE A: NL-word coincidence must NOT beat structural degree --------


def test_nl_word_does_not_beat_degree(tmp_path: Path) -> None:
    """``start`` (ref=1, matches the NL word "start" in the issue) must NOT lead
    over ``new`` (ref=3, structurally central). The issue contains NO backticked
    ``start`` — it is a prose word, not a code symbol.

    RED on the old absolute ``CASE WHEN name IN (anchors) THEN 0`` hoist (start
    leads); GREEN once NL-word match is demoted to a tiebreak under degree.
    """
    F = "span.rs"
    nodes = [
        (1, "Function", "start", F),  # the word-coincidence, ref=1
        (2, "Function", "new", F),  # the central function, ref=3
        (10, "Function", "caller_a", "a.rs"),
        (11, "Function", "caller_b", "b.rs"),
        (12, "Function", "caller_c", "c.rs"),
        (13, "Function", "caller_d", "d.rs"),
    ]
    # new <- 3 callers (ref=3); start <- 1 caller (ref=1)
    edges = [(10, 2), (11, 2), (12, 2), (13, 1)]
    db = _build(tmp_path, nodes, edges)

    # `start` is an NL word from the issue, NOT a backticked code symbol.
    nl_words = {"start", "code", "point"}
    code_symbols: set[str] = set()  # no backticked symbols in the issue

    names = _top_function_names(db, F, issue_terms=nl_words, code_symbols=code_symbols)
    assert names[0] == "new", (
        f"NL-word coincidence beat structural degree: order={names!r} "
        "(expected 'new' first, the ref=3 central function)"
    )

    # Same invariant on the signature-returning twin.
    sigs = _top_functions(db, F, issue_terms=nl_words, code_symbols=code_symbols)
    assert "new" in sigs[0], f"_top_functions hoisted the word-coincidence: {sigs!r}"


def test_code_symbol_anchor_still_surfaces_low_ref(tmp_path: Path) -> None:
    """importer.py-regression guard: a low-ref function whose name is a backticked
    CODE SYMBOL in the issue (``set_fields``) MUST still hoist over the central
    function — this is exactly what the anchor-first sort was built for.

    If this goes RED, the fix over-corrected and killed the set_fields case.
    """
    F = "importer.py"
    nodes = [
        (1, "Function", "set_fields", F),  # the edit target, ref=0
        (2, "Function", "run", F),  # the hub, ref=5
        (20, "Function", "h1", "h1.py"),
        (21, "Function", "h2", "h2.py"),
        (22, "Function", "h3", "h3.py"),
        (23, "Function", "h4", "h4.py"),
        (24, "Function", "h5", "h5.py"),
    ]
    edges = [(20, 2), (21, 2), (22, 2), (23, 2), (24, 2)]  # run <- 5 callers
    db = _build(tmp_path, nodes, edges)

    # `set_fields` IS a backticked code symbol in the issue (high-confidence).
    code_symbols = {"set_fields"}
    nl_words = {"set_fields", "field", "values"}

    names = _top_function_names(db, F, issue_terms=nl_words, code_symbols=code_symbols)
    assert names[0] == "set_fields", (
        f"code-symbol anchor (ref=0 edit target) did NOT surface: {names!r}"
    )

    sigs = _top_functions(db, F, issue_terms=nl_words, code_symbols=code_symbols)
    assert "set_fields" in sigs[0], f"_top_functions dropped the code symbol: {sigs!r}"


def test_no_anchors_falls_back_to_degree(tmp_path: Path) -> None:
    """With neither code symbols nor NL words, order is pure ``ref_count DESC`` —
    backward-compatible no-op for positional callers."""
    F = "m.py"
    nodes = [
        (1, "Function", "low", F),
        (2, "Function", "high", F),
        (30, "Function", "c1", "x.py"),
        (31, "Function", "c2", "y.py"),
    ]
    edges = [(30, 2), (31, 2)]  # high <- 2 callers
    db = _build(tmp_path, nodes, edges)
    names = _top_function_names(db, F)
    assert names[0] == "high", f"degree fallback broke: {names!r}"


# --- ROOT CAUSE C: vendored / demo paths are demoted from candidates ----------


def test_vendored_and_demo_paths_are_demotable() -> None:
    """The exact noise paths from the live runs must be caught by the SAME
    predicates the brief uses to drop candidates — no new ad-hoc list."""
    # benchmark/libs is caught by is_test_or_demo (segment 'benchmark').
    assert is_test_or_demo("benchmark/libs/mashumaro/common.py")
    assert is_test_or_demo("benchmark/libs/dacite/common.py")
    # examples/** is caught by is_test_or_demo (segment 'examples').
    assert is_test_or_demo("examples/units/qunit.js")
    # vendored dirs are caught by is_vendored_path.
    assert is_vendored_path("third_party/foo/bar.py")
    assert is_vendored_path("a/node_modules/pkg/index.js")
    # a normal source file is NOT demoted (correct-or-quiet: no over-reach).
    assert not is_test_or_demo("src/groundtruth/pretask/v1r_brief.py")
    assert not is_vendored_path("src/groundtruth/pretask/v1r_brief.py")


# --- ROOT CAUSE B: the #1 file keeps the graph-map lead via a fallback focus ---


def _map_schema_db(tmp_path: Path) -> str:
    db = str(tmp_path / "mapb.db")
    conn = sqlite3.connect(db)
    conn.executescript(_BLEND_SCHEMA)
    # File A (#1 / gold): rank-0 focus `quiet` has NO callers (abstains, empty
    # fact-map); rank-1 `central` HAS a verified caller (visible).
    # File B (#2): `g` has a verified caller (visible).
    conn.executemany(
        "INSERT INTO nodes (id, label, name, file_path) VALUES (?,?,?,?)",
        [
            (1, "Function", "quiet", "a.py"),
            (2, "Function", "central", "a.py"),
            (3, "Function", "g", "b.py"),
            (10, "Function", "ca", "caller_a.py"),
            (11, "Function", "cb", "caller_b.py"),
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
        "VALUES (?,?,'CALLS','import',1.0)",
        [(10, 2), (11, 3)],  # central <- ca (visible); g <- cb (visible); quiet: none
    )
    conn.commit()
    conn.close()
    return db


def test_top_file_keeps_graph_map_lead_via_fallback_focus(tmp_path: Path) -> None:
    """The #1 localizer file's lead is SOVEREIGN: when its rank-0 focus abstains
    (empty fact-map), the lead must NOT cede to a lower-ranked file — the #1 file
    keeps it via its NEXT structurally-central function.

    RED before the fix (render_map skipped a.py's empty `quiet` map and led with
    b.py); GREEN after the per-file fallback-focus chain picks a.py::central.
    """
    db = _map_schema_db(tmp_path)
    files = [
        FileEntry(path="a.py", score=0.99, function_names=["quiet", "central"]),
        FileEntry(path="b.py", score=0.80, function_names=["g"]),
    ]
    out = render_brief(files, graph_db=db)
    assert "<gt-graph-map>" in out, "no map rendered at all"
    map_start = out.find("<gt-graph-map>")
    map_end = out.find("</gt-graph-map>")
    block = out[map_start:map_end]
    a_pos = block.find("a.py ::")
    b_pos = block.find("b.py ::")
    assert a_pos != -1, f"#1 file a.py absent from the leading map: {block!r}"
    assert a_pos < b_pos or b_pos == -1, (
        f"lower-ranked b.py led the map over the #1 file a.py: {block!r}"
    )
    # The fallback focus surfaced a.py's central (visible) function, not `quiet`.
    assert "a.py :: central" in block, f"fallback focus not used: {block!r}"


def test_good_case_top_file_first_function_still_leads(tmp_path: Path) -> None:
    """Regression guard: when the #1 file's rank-0 focus IS visible, it still leads
    with that function — the fallback must not perturb the good case."""
    db = str(tmp_path / "good.db")
    conn = sqlite3.connect(db)
    conn.executescript(_BLEND_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id, label, name, file_path) VALUES (?,?,?,?)",
        [
            (1, "Function", "central", "a.py"),  # rank-0, visible
            (2, "Function", "g", "b.py"),
            (10, "Function", "ca", "caller_a.py"),
        ],
    )
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
        "VALUES (10,1,'CALLS','import',1.0)"
    )
    conn.commit()
    conn.close()
    files = [
        FileEntry(path="a.py", score=0.99, function_names=["central"]),
        FileEntry(path="b.py", score=0.80, function_names=["g"]),
    ]
    out = render_brief(files, graph_db=db)
    block = out[out.find("<gt-graph-map>") : out.find("</gt-graph-map>")]
    assert "a.py :: central" in block, f"good case broke: {block!r}"


# --- ROOT CAUSE C (E2E): a vendored candidate never reaches the brief entries --


@patch("groundtruth.pretask.v1r_brief.run_v74")
def test_vendored_candidate_dropped_from_brief_entries(mock_v74: MagicMock, tmp_path: Path) -> None:
    """A vendored candidate that the UPSTREAM demo-segment filter does NOT catch
    (``third_party/`` is a vendor-dir marker, not a demo segment) must still be
    dropped by the fail-closed chokepoint before the brief's entries are built, so
    a vendored copy is never offered as an edit target. The real source file is
    kept (correct-or-quiet: the guard demotes only the path class).

    RED before the chokepoint (third_party leaked through path-rescue/injection);
    GREEN after.
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "third_party" / "dacite").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "common.py").write_text("def common():\n    pass\n", encoding="utf-8")
    (repo / "third_party" / "dacite" / "common.py").write_text(
        "def common():\n    pass\n", encoding="utf-8"
    )
    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(_BLEND_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id, label, name, file_path) VALUES (?,?,?,?)",
        [
            (1, "Function", "common", "src/common.py"),
            (2, "Function", "common", "third_party/dacite/common.py"),
        ],
    )
    conn.commit()
    conn.close()

    # Both files are candidates; the vendored one even scores HIGHER (the exact
    # leak shape — a vendored dup out-ranking the real file).
    mock_v74.return_value = MagicMock(
        ranked_full=[
            {"path": "third_party/dacite/common.py", "score": 0.95, "components": {"path": 0.9}},
            {"path": "src/common.py", "score": 0.80, "components": {"path": 0.0}},
        ]
    )
    result = generate_v1r_brief("fix common() handling", str(repo), db)
    assert "third_party/dacite/common.py" not in result.brief_text, (
        "vendored copy leaked into the brief as an edit target"
    )
    assert "src/common.py" in result.brief_text, (
        "the real source file was wrongly dropped (over-reach)"
    )


# --- ROOT CAUSE C (scope chain): vendored/demo files never enter "check ALL" ----


def test_vendored_file_dropped_from_scope_chain(tmp_path: Path) -> None:
    """A pure-vendored-dir file (``third_party/``) connected into a scope chain must
    NOT be rendered in the 'Scope chain (graph-connected, check ALL)' directive — the
    worst place for noise, since it tells the agent to inspect a third-party file. The
    chain must honor the SAME deliverable-path predicates the rest of the brief uses.

    The biting path is ``third_party/`` specifically: ``is_test_path`` (the chain's only
    pre-fix filter) does NOT catch it, but ``is_vendored_path`` does — so before the fix
    it leaked into 'check ALL', after the fix it is dropped. (node_modules/examples were
    already caught by is_test_path; third_party is the class this fix actually closes.)

    RED before the fix (chain filtered is_test_path only → third_party rendered);
    GREEN after it also drops is_vendored_path + is_test_or_demo.
    """
    import types

    db = str(tmp_path / "chain.db")
    conn = sqlite3.connect(db)
    conn.executescript(_BLEND_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id, label, name, file_path) VALUES (?,?,?,?)",
        [(1, "Function", "f", "src/a.js"), (2, "Function", "g", "src/b.js")],
    )
    conn.commit()
    conn.close()
    files = [FileEntry(path="src/a.js", score=0.99, function_names=["f"])]
    # third_party/ FIRST so it renders visibly (not truncated) when the filter misses it
    # the description ALSO references the vendored node — the witnessed leak shape
    # (`qunit.js -> reporter.js`), which the basename filter alone does NOT scrub.
    chain = types.SimpleNamespace(
        files=[
            "third_party/jquery/qunit.js",
            "src/a.js",
            "src/b.js",
        ],
        description="qunit.js -> b.js (emit -> fn); a.js -> b.js (run -> emit)",
        confidence=0.9,
    )
    out = render_brief(files, graph_db=db, scope_chains=[chain])
    chain_lines = [ln for ln in out.splitlines() if "Scope chain" in ln]
    assert chain_lines, f"scope chain not rendered at all: {out!r}"
    chain_line = chain_lines[0]
    # the two real source files survive (>=2 → chain still emits)
    assert "a.js" in chain_line and "b.js" in chain_line, (
        f"real source files dropped from the chain: {chain_line!r}"
    )
    # the third_party vendored file is gone from the basename chain
    assert "qunit.js" not in chain_line, (
        f"third_party vendored file leaked into 'check ALL': {chain_line!r}"
    )
    # AND gone from the "Chain:" description (the real delivered leak) — the
    # qunit.js segment is scrubbed, the source-only segment survives
    desc_lines = [ln for ln in out.splitlines() if ln.strip().startswith("Chain:")]
    if desc_lines:
        desc_line = desc_lines[0]
        assert "qunit.js" not in desc_line, (
            f"vendored node leaked into the Chain: description: {desc_line!r}"
        )
        assert "a.js -> b.js" in desc_line, (
            f"source-only segment wrongly scrubbed from Chain: {desc_line!r}"
        )
