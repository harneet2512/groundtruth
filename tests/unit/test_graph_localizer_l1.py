"""TTD (artifact-first, red->green) for the symbol-anchored graph-witness L1.

REFERENCE ARTIFACT — real beets-5495 gt_run_summary:
    l1_candidate_files = ['beets/util/pipeline.py', 'beets/library.py']
    gold beets/importer.py is NOT a candidate
    candidates had 0 call/import/test edges, l1_confidence_score = 0.00
    yet a "Highest-confidence candidate" line still rendered.

ROOT CAUSE: the L1 ranker selected candidates by LEXICAL keyword overlap and
never TRAVERSED graph.db, so it missed importer.py even though
importer.py::set_fields has a CALLS edge to dbcore/db.py::set_parse — the symbol
pair the issue names.

This file builds a synthetic beets-shaped graph.db reproducing that topology:
  * beets/importer.py :: set_fields   CALLS   beets/dbcore/db.py :: set_parse
    (deterministic edge -> verified witness)
  * beets/util/pipeline.py and beets/library.py exist with NO edge to the
    anchored symbols (lexical match to issue keywords only).
Issue text anchors to set_fields / set_parse / "parse" / "field".

RED (pre-fix): rendered brief ranks [pipeline.py, library.py]; importer.py
    absent OR a 0.0-confidence confident directive present.
GREEN (post-fix): the localizer surfaces importer.py as the TOP candidate via
    its set_fields->set_parse witness, ranked above the witness-less files; a
    no-anchor variant SUPPRESSES the confident directive with the grep fallback;
    negative control: a witness-less lexical-only candidate never gets the
    "highest-confidence" line.

No AI anywhere — pure sqlite + regex.
"""
from __future__ import annotations

import sqlite3

import pytest

from groundtruth.pretask.graph_localizer import localize
from groundtruth.pretask.v1r_brief import (
    FileEntry,
    _entry_confidence_tier,
    generate_v1r_brief,
    render_brief,
)

# --- issue text: anchors to set_fields / set_parse / parse / field ----------
_BEETS_ISSUE = (
    "set_fields does not parse values correctly. When calling set_fields on an "
    "item, the field string is stored verbatim instead of being parsed by "
    "set_parse. Expected set_parse to coerce the field value."
)


def _make_beets_db(tmp_path):
    """Synthetic beets-shaped graph.db + repo files.

    importer.py::set_fields --CALLS(verified)--> dbcore/db.py::set_parse
    pipeline.py / library.py: defined, lexical keyword match, NO edge to anchors.
    """
    repo = tmp_path / "repo"
    (repo / "beets" / "dbcore").mkdir(parents=True)
    (repo / "beets" / "util").mkdir(parents=True)

    (repo / "beets" / "importer.py").write_text(
        "def set_fields(self, fields):\n"
        "    for key, val in fields.items():\n"
        "        self.set_parse(key, val)\n",
        encoding="utf-8",
    )
    (repo / "beets" / "dbcore" / "db.py").write_text(
        "def set_parse(self, key, string):\n"
        "    return _parse(string)\n",
        encoding="utf-8",
    )
    # Hard negatives: lexically mention parse/field/values but no edge to anchors.
    (repo / "beets" / "util" / "pipeline.py").write_text(
        "def parse_stage(values):\n"
        "    # parse the field values in the pipeline\n"
        "    return values\n",
        encoding="utf-8",
    )
    (repo / "beets" / "library.py").write_text(
        "def store(self, fields):\n"
        "    # library stores parsed field values\n"
        "    return fields\n",
        encoding="utf-8",
    )

    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, metadata TEXT
        );
        """
    )
    # nodes
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,signature,"
        "is_test,language) VALUES (?,?,?,?,?,?,?,0,'python')",
        [
            (1, "Method", "set_fields", "beets/importer.py", 1, 3,
             "def set_fields(self, fields):"),
            (2, "Method", "set_parse", "beets/dbcore/db.py", 1, 2,
             "def set_parse(self, key, string):"),
            (3, "Function", "parse_stage", "beets/util/pipeline.py", 1, 3,
             "def parse_stage(values):"),
            (4, "Method", "store", "beets/library.py", 1, 3,
             "def store(self, fields):"),
        ],
    )
    # The ONE structural witness: set_fields CALLS set_parse, deterministic edge.
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES "
        "(1,1,2,'CALLS',3,'beets/importer.py','import',1.0)"
    )
    conn.commit()
    conn.close()
    return str(repo), db


# ===========================================================================
# 1. localize() — ANCHOR -> TRAVERSE -> RERANK -> GATE (unit on the core)
# ===========================================================================

def test_localize_surfaces_importer_as_top_via_witness(tmp_path):
    """GREEN: importer.py is the top candidate via its set_fields->set_parse
    witness, ranked ABOVE the witness-less lexical hard negatives."""
    _repo, db = _make_beets_db(tmp_path)
    res = localize(_BEETS_ISSUE, db)
    assert res.candidates, "localizer found no candidates on the witnessed db"
    top = res.candidates[0]
    assert top.file_path == "beets/importer.py", (
        f"expected importer.py top, got {top.file_path}; "
        f"order={[c.file_path for c in res.candidates]}"
    )
    assert top.has_verified_witness, "top candidate must carry a verified witness"
    # The witness must name the real edge.
    wit = top.render_witness().lower()
    assert "set_fields" in wit and "set_parse" in wit, wit
    # SWERank hard-negative: importer.py outranks pipeline.py / library.py.
    order = [c.file_path for c in res.candidates]
    assert order.index("beets/importer.py") == 0


def test_localize_confident_gate_fires_on_verified_witness(tmp_path):
    _repo, db = _make_beets_db(tmp_path)
    res = localize(_BEETS_ISSUE, db)
    assert res.confident, f"expected confident, gate_reason={res.gate_reason}"
    assert res.confidence > 0.0


def test_localize_no_anchor_returns_empty_nonconfident(tmp_path):
    """No issue symbol resolves to a graph node -> empty, not confident
    (correct-or-quiet)."""
    _repo, db = _make_beets_db(tmp_path)
    res = localize("the application crashes sometimes with weird output", db)
    assert res.candidates == []
    assert res.confident is False
    assert res.confidence == 0.0
    assert res.gate_reason in ("no_anchor_hit", "no_witness")


# ===========================================================================
# 2. render_brief() PLUMBING — the rendered string is the deliverable
# ===========================================================================

def test_render_witnessed_top_shows_importer_with_witness_and_confident_line():
    """GREEN plumbing: rendered brief surfaces importer.py with its witness AND
    the confident line fires (because the witness is verified)."""
    importer = FileEntry(
        path="beets/importer.py", score=0.6, functions=["set_fields"],
        function_names=["set_fields"],
        witness="set_fields calls set_parse [CALLS]",
        witness_verified=True, localizer_confidence=1.0,
    )
    pipeline = FileEntry(
        path="beets/util/pipeline.py", score=0.9, functions=["parse_stage"],
        function_names=["parse_stage"],
    )
    out = render_brief(
        [importer, pipeline], scores=[0.6, 0.9], issue_text=_BEETS_ISSUE,
    )
    # importer.py is ranked #1 in the rendered list.
    assert "1. beets/importer.py" in out, out
    # Its witness is delivered (appears in the rendered string, not just computed).
    assert "set_fields calls set_parse" in out, out
    # The confident line names importer.py with the graph witness.
    assert "Highest-confidence candidate" in out
    assert "graph witness: set_fields calls set_parse" in out


def test_render_witnessless_lexical_only_never_gets_confident_line():
    """NEGATIVE CONTROL / faithful RED->GREEN: a witness-less file that the OLD
    gate would have rendered as the confident answer (it was [VERIFIED]-tier via
    issue-match+contract AND high score gap) must NOT get the 'highest-confidence'
    line under the NEW gate, because it carries NO verified graph witness.

    This is the exact beets-5495 harm: pipeline.py was lexically ranked #1 and
    rendered as the confident answer with 0.0 structural confidence.

    PRE-FIX (old gate `high_confidence and tier==[VERIFIED]`): this fires the
    confident line -> RED.
    POST-FIX (gate requires a verified witness OR localizer-silent): suppressed.
    """
    # set_fields is in the issue text AND a contract is present -> OLD tier was
    # [VERIFIED]; score gap 0.9 vs 0.3 is > 0.3 -> OLD high_confidence True.
    pipeline = FileEntry(
        path="beets/util/pipeline.py", score=0.9, functions=["set_fields"],
        function_names=["set_fields"],
        contract="beets/other.py:55",  # present -> issue_match+contract => [VERIFIED]
        # NO witness / witness_verified=False (this is the whole point).
    )
    library = FileEntry(
        path="beets/library.py", score=0.3, functions=["store"],
        function_names=["store"],
    )
    out = render_brief(
        [pipeline, library], scores=[0.9, 0.3], issue_text=_BEETS_ISSUE,
        graph_db="",  # no graph -> cannot prove a name_match-only weakness
    )
    assert "Highest-confidence candidate" not in out, (
        "confident line fired on a witness-less lexical-only candidate "
        "(the beets-5495 harm is NOT closed):\n" + out
    )


def test_render_no_verified_witness_emits_grep_fallback():
    """When no candidate carries a verified witness and none is [VERIFIED], the
    honest grep fallback is present."""
    pipeline = FileEntry(
        path="beets/util/pipeline.py", score=0.9, functions=["parse_stage"],
        function_names=["parse_stage"],
    )
    library = FileEntry(
        path="beets/library.py", score=0.85, functions=["store"],
        function_names=["store"],
    )
    out = render_brief(
        [pipeline, library], scores=[0.9, 0.85], issue_text=_BEETS_ISSUE,
        graph_db="",
    )
    assert "could not anchor" in out.lower() and "grep" in out.lower(), out


def test_tier_verified_witness_is_verified_tier():
    """A verified graph-traversal witness earns [VERIFIED] on its own."""
    e = FileEntry(
        path="beets/importer.py", score=0.6, functions=["set_fields"],
        function_names=["set_fields"],
        witness="set_fields calls set_parse [CALLS]", witness_verified=True,
    )
    assert _entry_confidence_tier(e, _BEETS_ISSUE) == "[VERIFIED]"


def test_tier_unverified_witness_is_warning_not_verified():
    """A name_match witness is real-but-weak -> [WARNING], never [VERIFIED]."""
    e = FileEntry(
        path="beets/x.py", score=0.6, functions=["foo"], function_names=["foo"],
        witness="foo calls bar [CALLS (unverified)]", witness_verified=False,
    )
    assert _entry_confidence_tier(e, "unrelated issue text") == "[WARNING]"


# ===========================================================================
# 3. generate_v1r_brief() END-TO-END — plumbing through the live brief path
# ===========================================================================

def test_generate_v1r_brief_surfaces_importer_top_with_witness(tmp_path):
    """End-to-end through the LIVE brief path: the rendered <gt-task-brief>
    contains importer.py as the top/highest-confidence candidate WITH its witness.

    This is the plumbing proof the task demands: 'Delivered' = appears in the
    rendered brief output, not just computed internally.
    """
    repo, db = _make_beets_db(tmp_path)
    result = generate_v1r_brief(_BEETS_ISSUE, repo, db, bug_id="beets-5495-synth")
    brief = result.brief_text
    assert "beets/importer.py" in brief, (
        "importer.py absent from rendered brief (the beets-5495 failure):\n" + brief
    )
    # Witness delivered in the rendered string.
    assert "set_parse" in brief, (
        "set_fields->set_parse witness not delivered in brief:\n" + brief
    )
    # importer.py ranks ahead of the lexical hard negatives in the rendered list.
    paths_in_order = [e.path for e in result.files]
    assert "beets/importer.py" in paths_in_order
    imp_idx = paths_in_order.index("beets/importer.py")
    for hn in ("beets/util/pipeline.py", "beets/library.py"):
        if hn in paths_in_order:
            assert imp_idx < paths_in_order.index(hn), (
                f"{hn} (witness-less) ranked above importer.py (witnessed): "
                f"{paths_in_order}"
            )


def test_generate_v1r_brief_no_anchor_suppresses_confident_line(tmp_path):
    """No-anchor input through the live path: confident line absent, grep
    fallback present (or at minimum no confident directive)."""
    repo, db = _make_beets_db(tmp_path)
    result = generate_v1r_brief(
        "the program produces unexpected output occasionally", repo, db,
        bug_id="no-anchor-synth",
    )
    brief = result.brief_text
    # The confident line must NOT name a candidate on a no-anchor task.
    assert "Highest-confidence candidate" not in brief, (
        "confident directive fired on a no-anchor task:\n" + brief
    )


# ===========================================================================
# 4. CATEGORICAL ADMISSION FILTER — single-source-of-truth edge gating in the
#    BFS. Reuses curation_map.py:113: admit IFF FACT (deterministic method) OR
#    confidence >= _NAME_MATCH_FLOOR (0.5); trust_tier='SUPPRESSED' HARD-EXCLUDED.
#    Pillar 4 (.claude/CLAUDE.md:24): "confidence-gated AT THE FILTER LEVEL".
#    RED (pre-fix): the BFS admitted every CALLS/IMPORTS edge, so a conf-0.2 or a
#    SUPPRESSED-tier name_match edge surfaced junk.py as an (unverified) candidate.
#    GREEN (post-fix): such edges are dropped at admission; junk.py never appears.
# ===========================================================================

def _make_db_with_noise_edge(tmp_path, *, conf, method, tier, with_tier_col):
    """beets db (importer.py verified-witnessed) PLUS a junk.py reachable ONLY by a
    single noise edge set_fields->junk_fn carrying (method, conf, tier).
    """
    repo, db = _make_beets_db(tmp_path)  # importer/db/pipeline/library + verified edge
    conn = sqlite3.connect(db)
    if with_tier_col:
        # legacy fixture has no trust_tier column; add it to exercise the SUPPRESSED path.
        try:
            conn.execute("ALTER TABLE edges ADD COLUMN trust_tier TEXT")
        except sqlite3.Error:
            pass
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,signature,"
        "is_test,language) VALUES (5,'Function','junk_fn','beets/junk.py',1,2,"
        "'def junk_fn():',0,'python')"
    )
    if with_tier_col:
        conn.execute(
            "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
            "resolution_method,confidence,trust_tier) VALUES "
            "(2,1,5,'CALLS',2,'beets/importer.py',?,?,?)",
            (method, conf, tier),
        )
    else:
        conn.execute(
            "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
            "resolution_method,confidence) VALUES "
            "(2,1,5,'CALLS',2,'beets/importer.py',?,?)",
            (method, conf),
        )
    conn.commit()
    conn.close()
    return repo, db


def test_admission_drops_low_confidence_name_match(tmp_path):
    """A name_match edge with confidence < _NAME_MATCH_FLOOR (0.5) is dropped at
    admission -> junk.py never becomes an (unverified) candidate. importer.py (the
    verified witness) is unaffected."""
    _repo, db = _make_db_with_noise_edge(
        tmp_path, conf=0.2, method="name_match", tier=None, with_tier_col=False,
    )
    res = localize(_BEETS_ISSUE, db)
    paths = [c.file_path for c in res.candidates]
    assert "beets/junk.py" not in paths, f"low-conf name_match leaked: {paths}"
    assert "beets/importer.py" in paths, f"verified witness lost: {paths}"


def test_admission_hard_excludes_suppressed_trust_tier(tmp_path):
    """A trust_tier='SUPPRESSED' edge is HARD-EXCLUDED even when its confidence is
    ABOVE the floor (0.9) -> the tier hard-exclude is independent of the conf gate.
    junk.py must not surface; importer.py still does."""
    _repo, db = _make_db_with_noise_edge(
        tmp_path, conf=0.9, method="name_match", tier="SUPPRESSED", with_tier_col=True,
    )
    res = localize(_BEETS_ISSUE, db)
    paths = [c.file_path for c in res.candidates]
    assert "beets/junk.py" not in paths, f"SUPPRESSED-tier edge leaked: {paths}"
    assert "beets/importer.py" in paths, f"verified witness lost: {paths}"


def test_admission_keeps_midconf_name_match_above_floor(tmp_path):
    """Boundary / no over-suppression: a name_match edge with confidence >= floor
    (0.6, the 2-candidate case) is STILL admitted as an (unverified) witness ->
    junk.py surfaces (just never as the confident top). Proves the filter drops
    only BELOW the floor, not all name_match."""
    _repo, db = _make_db_with_noise_edge(
        tmp_path, conf=0.6, method="name_match", tier=None, with_tier_col=False,
    )
    res = localize(_BEETS_ISSUE, db)
    paths = [c.file_path for c in res.candidates]
    assert "beets/junk.py" in paths, f"mid-conf name_match wrongly suppressed: {paths}"


def test_render_witness_prefers_meaningful_over_generic():
    """render_witness must DISPLAY the issue-relevant edge, not an arbitrary
    generic constructor edge. Live beets-5495 bug: the brief rendered
    '__init__ called by _setup_logging' (both hop-0 verified, tie on strength)
    and hid the real 'set_fields calls set_parse'."""
    from groundtruth.pretask.graph_localizer import Candidate, Witness
    generic = Witness(
        file_path="beets/importer.py", anchor="x", edge_type="CALLS",
        direction="called_by_anchor", verified=True, confidence=1.0, hop=0,
        src_symbol="__init__", dst_symbol="_setup_logging",
    )
    meaningful = Witness(
        file_path="beets/importer.py", anchor="set_fields", edge_type="CALLS",
        direction="calls_anchor", verified=True, confidence=1.0, hop=0,
        src_symbol="set_fields", dst_symbol="set_parse",
    )
    c = Candidate(
        file_path="beets/importer.py", score=1.0, witnesses=[generic, meaningful],
        lex_hits=3, degree=5, confidence=1.0,
    )
    out = c.render_witness()
    assert "set_fields" in out and "set_parse" in out, out
    assert "__init__" not in out, f"rendered the generic witness: {out}"
