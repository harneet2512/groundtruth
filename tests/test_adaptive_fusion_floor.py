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


# ════════════ DIM-1 COMPOSES VIA MAX UNDER identifier_heavy (P0 fix 2026-06-09) ═══════════


def test_dim1_keeps_lexical_lead_on_backtick_symbol_issue():
    """RED before the fix: a backtick-symbol issue (identifier_heavy -> Dim-0
    floors W_LEX up to 0.65 / W_PATH to 0.55) that ALSO resolves code_def_scores
    triggered Dim-1's direct assignment (W_LEX=0.35 / W_PATH=0.30), silently
    revoking the lexical lead. Dim-1 may only RAISE W_LEX/W_PATH here."""
    anc = IssueAnchors(code_symbols={"trusted_hosts"})
    w = _adapt_weights_for_issue(
        {},  # no frames
        {"app/wrappers.py": 1.0},  # code_def signal present -> Dim-1 fires
        dict(DEFAULT_WEIGHTS),
        issue_text="the value returned by `request.trusted_hosts` is wrong",
        issue_anchors=anc,
    )
    assert w["W_LEX"] >= 0.65, f"lexical lead revoked: W_LEX={w['W_LEX']}"
    assert w["W_PATH"] >= 0.55, f"path lead revoked: W_PATH={w['W_PATH']}"
    assert w["W_CODE_DEF"] == pytest.approx(0.70)  # Dim-1's own signal still applied
    assert w["W_SEM"] == pytest.approx(_w_sem_floor())  # dense floored, not zeroed


def test_dim1_keeps_lexical_lead_with_frames_on_identifier_heavy():
    """Same property on the frames branch: an identifier_heavy issue with a
    resolvable traceback keeps W_LEX >= 0.65 (Dim-1 raises only)."""
    w = _adapt_weights_for_issue(
        {"src/mod.py": 1.0},  # frame signal present -> Dim-1 frames branch
        {},
        dict(DEFAULT_WEIGHTS),
        issue_text="lint error E1010 raised here",
        issue_anchors=IssueAnchors(),
    )
    assert w["W_LEX"] >= 0.65
    assert w["W_PATH"] >= 0.55
    assert w["W_FRAME"] == pytest.approx(0.80)


def test_dim1_direct_assignment_preserved_off_identifier_heavy():
    """No-regression: off identifier_heavy (mixed), Dim-1's documented direct
    assignment stands byte-identical (frames demote lexical to let frame lead)."""
    w = _adapt_weights_for_issue(
        {"src/mod.py": 1.0}, {}, dict(DEFAULT_WEIGHTS),
        issue_text="broken", issue_anchors=IssueAnchors(),
    )
    assert w["W_LEX"] == pytest.approx(0.30)
    assert w["W_PATH"] == pytest.approx(0.25)
    assert w["W_FRAME"] == pytest.approx(0.80)


# ════════════ DIM-3 SINGLE-SOURCES THE DETERMINISTIC SET (fix 2026-06-09) ═════════════


@pytest.fixture
def graph_db_impl_method(tmp_path: Path) -> str:
    """A graph whose CALLS edges are ALL resolution_method='impl_method' — a
    deterministic method per curation_map.DETERMINISTIC_RESOLUTION_METHODS that
    the old hand-rolled Dim-3 subset omitted."""
    db = tmp_path / "graph_impl.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, language, is_test) "
        "VALUES ('Function', 'fn_a', 'app/a.py', 'python', 0)"
    )
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, language, is_test) "
        "VALUES ('Function', 'fn_b', 'app/b.py', 'python', 0)"
    )
    for _ in range(10):
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
            "VALUES (1, 2, 'CALLS', 'impl_method', 0.95)"
        )
    conn.commit()
    conn.close()
    return str(db)


def test_dim3_counts_impl_method_as_deterministic(graph_db_impl_method: str):
    """RED before the fix: Dim-3 hand-rolled a 7-method literal WITHOUT
    impl_method/inherited/unique_method/return_type, so a 100%-impl_method graph
    measured det_pct=0 (<0.30) and W_REACH was REDUCED to 0.03. With the shared
    curation_map set, det_pct=1.0 (>0.70) and W_REACH is boosted to 0.12."""
    w = _adapt_weights_for_issue(
        {}, {}, dict(DEFAULT_WEIGHTS),
        graph_db=graph_db_impl_method,
        issue_anchors=IssueAnchors(),
        issue_text="broken",
    )
    assert w["W_REACH"] == pytest.approx(0.12), (
        f"impl_method edges not counted deterministic: W_REACH={w['W_REACH']}"
    )


# ════════════ SPARSE GRAPH: DENSE FLOORED, NEVER ZEROED (P0 fix 2026-06-09) ═══════════


def _minimal_v74_result():
    from groundtruth.pretask.v7_4_brief import V74BriefResult

    return V74BriefResult(
        bug_id="t", repo="r", hyperparameters={}, anchors=[], anchor_trust=[],
        candidate_set_size=0, ranked_top10_focus=[], ranked_full=[], focus_set=[],
        focus_set_size=0, gold_files=[], gold_in_focus=False,
        first_gold_rank_focus=None, first_gold_rank_full=None, ablation_variant="C",
    )


def test_sparse_graph_weights_floor_dense_not_zero(graph_db: str, monkeypatch):
    """RED before the fix: generate_v1r_brief's sparse-graph branch passed
    W_SEM=0.0 (dead off-proof — the floor silently resurrected it — and FATAL in
    proof+require: forbid_no_sem_config raised on every sparse repo). Per the
    locked §11.6 dense-floor policy the branch now passes W_SEM=floor: dense
    stays alive, lexical leads, graph signals stay zeroed."""
    from groundtruth.pretask import v1r_brief as v1r

    captured: dict = {}

    def _capture_run_v74(issue_text, repo_root, graph_db_, **kw):
        captured.update(kw)
        return _minimal_v74_result()

    monkeypatch.setattr(v1r, "run_v74", _capture_run_v74)
    # graph_db fixture has 2 files / 0 edges -> edges_per_file = 0 < 2.0 -> sparse.
    v1r.generate_v1r_brief("some issue text", ".", graph_db, bug_id="t")

    w = captured.get("weights")
    assert w is not None, "sparse-graph branch did not fire"
    floor = _w_sem_floor()
    assert w["W_SEM"] == pytest.approx(floor), f"dense not floored: W_SEM={w['W_SEM']}"
    assert w["W_SEM"] > 0.0, "dense hard-zeroed on a sparse graph (dead-or-fatal)"
    assert w["W_LEX"] == pytest.approx(0.70), "lexical must lead on sparse graphs"
    assert w["W_LEX"] > w["W_SEM"]
    assert w["W_REACH"] == 0.0 and w["W_PROX"] == 0.0 and w["W_HUB"] == 0.0


def test_forbid_no_sem_config_judges_post_adaptation_w_sem(
    graph_db: str, tmp_path: Path, monkeypatch
):
    """RED before the fix: run_v74 called proof.forbid_no_sem_config with the
    PRE-adaptation W_SEM (the caller's raw override — 0.0 on the sparse branch),
    aborting in proof+require even though the dense floor held. The gate must
    judge the POST-adaptation effective W_SEM (the weight actually applied)."""
    from groundtruth.pretask import v7_4_brief as b
    from groundtruth.runtime import proof

    seen: dict = {}
    monkeypatch.setattr(
        proof, "forbid_no_sem_config",
        lambda abl, rrf, w_sem: seen.setdefault("w_sem", float(w_sem)),
    )

    class _ZeroEnc:
        model_name = "fake/zero"
        dim = 384

        def encode(self, texts, **kw):
            import numpy as _np
            return _np.zeros((len(list(texts)), 384), dtype=_np.float32)

    monkeypatch.setattr(b, "_get_model", lambda: _ZeroEnc())
    monkeypatch.setattr(b, "_SEMANTIC_AVAILABLE", True)
    from groundtruth.pretask import anchor_select as _as
    _as._EMBED_CACHE.clear()
    _as._SYMVEC_CACHE.clear()

    b.run_v74(
        "some issue", str(tmp_path), graph_db,
        weights={"W_SEM": 0.0, "W_LEX": 0.70, "W_REACH": 0.0, "W_PROX": 0.0,
                 "W_HUB": 0.0, "W_COMMIT": 0.0, "W_PATH": 0.45},
    )
    assert "w_sem" in seen, "forbid_no_sem_config was not called"
    assert seen["w_sem"] >= _w_sem_floor() > 0.0, (
        f"gate judged the PRE-adaptation W_SEM ({seen['w_sem']}) — sparse repos "
        "would abort in proof+require even though the floor holds"
    )


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
