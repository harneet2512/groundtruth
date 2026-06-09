"""FUSION REDESIGN — query-adaptive weighting + the DENSE (W_SEM) floor.

Stage-1 deterministic validation for the fusion redesign in
``groundtruth.pretask.v7_4_brief``:

  * ``_classify_issue_lexicality`` — deterministic, generalized (no task IDs / no
    gold) classifier mapping an issue to ``identifier_heavy`` / ``nl_gap`` /
    ``mixed`` from issue_text + IssueAnchors.
  * Dimension 0 in ``_adapt_weights_for_issue`` — leads the linear-sum fusion
    toward the signal the query type favors WITHOUT throttling the other.
  * THE DENSE FLOOR — ``W_SEM >= W_SEM_FLOOR > 0`` after ALL weight adaptation,
    for EVERY classification (the guardrail + the proof-safety property:
    runtime/proof.py ``forbid_no_sem_config`` requires ``effective_w_sem > 0``).

Research basis (cited, not invented):
  BEIR — Thakur et al., NeurIPS 2021 Datasets & Benchmarks (hybrid dense+sparse;
    exact-term queries favor sparse lexical).
  Sciavolino et al., EMNLP 2021 — "Simple Entity-Centric Questions Challenge Dense
    Retrievers" (exact-identifier / entity queries favor lexical over dense).

All inputs are synthetic and generalized — no benchmark task IDs, gold files, or
repo-specific shapes. The classifier signals (rule-code regex, backtick code
symbols, graph-resolved paths, prose function-word ratio) are language-agnostic.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask.anchors import IssueAnchors
from groundtruth.pretask.v7_4_brief import (
    DEFAULT_WEIGHTS,
    _adapt_weights_for_issue,
    _classify_issue_lexicality,
    _w_sem_floor,
)

# A prose-heavy, identifier-free issue body (a "lexical gap" query): the bug is
# described entirely in natural language with no error code, no backtick symbol,
# no path. High closed-class-function-word ratio.
_PROSE_ISSUE = (
    "When the user submits the form the data is not saved and there is no error "
    "shown to them at all, so they have no idea that the thing they did was lost "
    "and they will try to do it again and again without any feedback from the page."
)


# ── graph.db fixture (for path-resolution + scope/confidence dims) ─────────────
_SCHEMA = """
    CREATE TABLE nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        name TEXT NOT NULL,
        qualified_name TEXT,
        file_path TEXT NOT NULL,
        start_line INTEGER,
        end_line INTEGER,
        signature TEXT,
        return_type TEXT,
        is_exported BOOLEAN DEFAULT 0,
        is_test BOOLEAN DEFAULT 0,
        language TEXT NOT NULL,
        parent_id INTEGER REFERENCES nodes(id)
    );
    CREATE TABLE edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        source_line INTEGER,
        source_file TEXT,
        resolution_method TEXT,
        confidence REAL DEFAULT 0.0,
        metadata TEXT
    );
"""


@pytest.fixture
def graph_db(tmp_path: Path) -> str:
    """A tiny indexed graph with one resolvable file (``app/importer.py``)."""
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, language, is_test) "
        "VALUES ('Function', 'read_item', 'app/importer.py', 'python', 0)"
    )
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, language, is_test) "
        "VALUES ('Function', 'helper', 'app/util.py', 'python', 0)"
    )
    conn.commit()
    conn.close()
    return str(db)


# ════════════════════════════ CLASSIFIER ══════════════════════════════════════


def test_classify_rule_code_is_identifier_heavy():
    """An error/rule code (E1010 — cfn-lint/pylint/tsc/rustc shape) -> identifier_heavy."""
    cls = _classify_issue_lexicality(
        "cfn-lint raises E1010 on this template and the rule is wrong",
        IssueAnchors(),
    )
    assert cls == "identifier_heavy"


def test_classify_various_rule_code_shapes_identifier_heavy():
    """Language-agnostic rule codes (pylint C0114, rustc E0599, flake8 W503)."""
    for code in ("C0114", "E0599", "W503", "E1010"):
        assert (
            _classify_issue_lexicality(f"the linter reports {code} here", IssueAnchors())
            == "identifier_heavy"
        ), code


def test_classify_backtick_code_symbol_is_identifier_heavy():
    """A backtick-wrapped code symbol (IssueAnchors.code_symbols) -> identifier_heavy."""
    anc = IssueAnchors(code_symbols={"trusted_hosts"})
    cls = _classify_issue_lexicality(
        "the value returned by `request.trusted_hosts` is wrong here", anc
    )
    assert cls == "identifier_heavy"


def test_classify_resolved_path_is_identifier_heavy(graph_db: str):
    """A path that RESOLVES against the indexed graph -> identifier_heavy."""
    anc = IssueAnchors(paths={"app/importer.py"})
    cls = _classify_issue_lexicality(
        "the bug is somewhere in this file please look", anc, graph_db=graph_db
    )
    assert cls == "identifier_heavy"


def test_classify_unresolved_path_does_not_force_identifier_heavy(graph_db: str):
    """A stray path that does NOT resolve against the graph must not misclassify a
    natural-language issue (correct-or-quiet path-resolution gate)."""
    anc = IssueAnchors(paths={"some/random/unindexed/elsewhere.py"})
    cls = _classify_issue_lexicality(_PROSE_ISSUE, anc, graph_db=graph_db)
    assert cls == "nl_gap"  # prose dominates; the unresolved path contributes nothing


def test_classify_pure_prose_is_nl_gap():
    """Identifier-free prose with a high function-word ratio -> nl_gap."""
    assert _classify_issue_lexicality(_PROSE_ISSUE, IssueAnchors()) == "nl_gap"


def test_classify_ambiguous_sparse_is_mixed():
    """Short / signal-less text -> mixed (no change, the no-regression bucket)."""
    assert _classify_issue_lexicality("broken", IssueAnchors()) == "mixed"
    assert _classify_issue_lexicality("", IssueAnchors()) == "mixed"


# ════════════════════════ THE DENSE FLOOR (the core ask) ══════════════════════


def _all_classification_cases(graph_db: str):
    """Every classification, each composed through the FULL adapter (all dims).

    Returns (label, issue_text, anchors, graph_db) tuples covering identifier_heavy,
    nl_gap, mixed — with and without a graph so Dimensions 2/3 also compose."""
    return [
        ("identifier_heavy/no-graph", "lint error E1010 here", IssueAnchors(), ""),
        (
            "identifier_heavy/code-symbol",
            "the `trusted_hosts` value is wrong",
            IssueAnchors(code_symbols={"trusted_hosts"}),
            "",
        ),
        (
            "identifier_heavy/resolved-path+graph",
            "see this file",
            IssueAnchors(paths={"app/importer.py"}, symbols={"read_item"}),
            graph_db,
        ),
        ("nl_gap/no-graph", _PROSE_ISSUE, IssueAnchors(), ""),
        ("nl_gap/graph", _PROSE_ISSUE, IssueAnchors(symbols={"read_item"}), graph_db),
        ("mixed/no-graph", "broken", IssueAnchors(), ""),
        ("mixed/graph", "broken", IssueAnchors(symbols={"read_item"}), graph_db),
    ]


def test_floor_invariant_holds_for_every_classification(graph_db: str):
    """THE GUARDRAIL + PROOF-SAFETY: for EVERY classification (incl. identifier_heavy
    and all dimensions composed), effective_w_sem >= W_SEM_FLOOR > 0.

    This is exactly the condition runtime/proof.py ``forbid_no_sem_config`` asserts
    (effective_w_sem > 0.0 under proof + require_embedder) — so this test proves the
    redesign is proof-safe by construction."""
    floor = _w_sem_floor()
    assert floor > 0.0
    for label, txt, anc, gdb in _all_classification_cases(graph_db):
        w = _adapt_weights_for_issue(
            {}, {}, dict(DEFAULT_WEIGHTS), graph_db=gdb, issue_anchors=anc, issue_text=txt
        )
        assert w["W_SEM"] >= floor > 0.0, f"{label}: W_SEM={w['W_SEM']} < floor={floor}"


def test_floor_holds_under_env_override(monkeypatch, graph_db: str):
    """GT_W_SEM_FLOOR raises the floor; the invariant still holds, still > 0."""
    monkeypatch.setenv("GT_W_SEM_FLOOR", "0.33")
    floor = _w_sem_floor()
    assert floor == pytest.approx(0.33)
    for label, txt, anc, gdb in _all_classification_cases(graph_db):
        w = _adapt_weights_for_issue(
            {}, {}, dict(DEFAULT_WEIGHTS), graph_db=gdb, issue_anchors=anc, issue_text=txt
        )
        assert w["W_SEM"] >= floor > 0.0, label


def test_invalid_env_override_falls_back_to_default():
    """A bad / out-of-range GT_W_SEM_FLOOR falls back to the 0.25 default (no crash)."""
    import os

    for bad in ("not-a-number", "-0.5", "0", "1.5", ""):
        os.environ["GT_W_SEM_FLOOR"] = bad
        try:
            assert _w_sem_floor() == 0.25, bad
        finally:
            os.environ.pop("GT_W_SEM_FLOOR", None)


# ════════════════════════ PER-CLASSIFICATION WEIGHT SHAPE ═════════════════════


def test_identifier_heavy_lexical_leads_and_sem_floored():
    """identifier_heavy: lexical LEADS (W_LEX >= W_SEM) AND dense is floored
    (W_SEM == W_SEM_FLOOR — led down to the floor, NOT zeroed)."""
    floor = _w_sem_floor()
    w = _adapt_weights_for_issue(
        {}, {}, dict(DEFAULT_WEIGHTS), issue_text="lint error E1010", issue_anchors=IssueAnchors()
    )
    assert w["W_LEX"] >= w["W_SEM"], "lexical must lead on identifier_heavy"
    assert w["W_SEM"] == pytest.approx(floor), "dense led DOWN to the floor (not zeroed)"
    assert w["W_SEM"] > 0.0
    assert w["W_LEX"] >= 0.65  # lexical floored up
    assert w["W_PATH"] >= 0.55  # path floored up


def test_nl_gap_dense_leads():
    """nl_gap: dense LEADS (W_SEM >= W_LEX) and lexical is NOT demoted below base."""
    w = _adapt_weights_for_issue(
        {}, {}, dict(DEFAULT_WEIGHTS), issue_text=_PROSE_ISSUE, issue_anchors=IssueAnchors()
    )
    assert w["W_SEM"] >= w["W_LEX"], "dense must lead on nl_gap"
    assert w["W_LEX"] >= DEFAULT_WEIGHTS["W_LEX"], "lexical must not be demoted (correct-or-quiet)"


def test_mixed_is_byte_identical_to_base_with_no_other_signal():
    """mixed + no frames/code-defs/graph -> byte-identical to the (new) base weights.

    The no-regression property for the mixed bucket: Dimension 0 adds nothing, and
    the floor leaves base W_SEM (0.40 >= 0.25) untouched. The ONLY intentional delta
    vs the pre-redesign ranker is the base W_SEM raise (0.15 -> 0.40), which is the
    deliberate dense-led default, not a per-issue regression."""
    base = dict(DEFAULT_WEIGHTS)
    w = _adapt_weights_for_issue(
        {}, {}, dict(DEFAULT_WEIGHTS), issue_text="broken", issue_anchors=IssueAnchors()
    )
    assert w == base, f"mixed must equal base; diff={ {k: (base.get(k), w.get(k)) for k in set(base)|set(w) if base.get(k) != w.get(k)} }"


def test_base_w_sem_is_dense_led():
    """The base W_SEM is a dense-led default (>= the floor and materially above the
    old e5-era 0.15) so dense leads on mixed/semantic queries."""
    assert DEFAULT_WEIGHTS["W_SEM"] == pytest.approx(0.40)
    assert DEFAULT_WEIGHTS["W_SEM"] >= _w_sem_floor() > 0.0


# ════════════════════════ EMBEDDER-OFF / ABLATION SAFETY ══════════════════════


def test_floor_does_not_resurrect_dead_sem_when_disabled():
    """enforce_floor=False (embedder absent / sem-zeroing ablation): a W_SEM already
    zeroed stays 0 — the floor must NOT resurrect a dead/ablated dense signal."""
    base = dict(DEFAULT_WEIGHTS)
    base["W_SEM"] = 0.0  # simulate the _SEMANTIC_AVAILABLE=False zeroing in run_v74
    for txt, anc in [
        ("lint error E1010", IssueAnchors()),
        (_PROSE_ISSUE, IssueAnchors()),
        ("broken", IssueAnchors()),
    ]:
        w = _adapt_weights_for_issue(
            {}, {}, base, issue_text=txt, issue_anchors=anc, enforce_floor=False
        )
        assert w["W_SEM"] == 0.0, f"floor must not resurrect dead sem ({txt!r})"
