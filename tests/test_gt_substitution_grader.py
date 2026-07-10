"""TTD for scripts/gt_substitution_grader.py — the acquisition-layer (W0) instrument.

Doctrine under test (gt_gt.md §24.4 RQ2): NON-RE-ACQUISITION is the true consumption
signal. If GT delivered "def foo -> src/mod.py:10" and the agent LATER greps "foo",
delivery FAILED (re-acquisition). If the search vanishes, the fact substituted.

RED-first: these tests target the grader BEFORE it exists (import fails => RED).
Fixtures live under tests/fixtures/subst_reacq_*/ (mini-swe-agent trajectory shape).
"""
from __future__ import annotations

import importlib.util
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_GRADER = os.path.join(os.path.dirname(_HERE), "scripts", "gt_substitution_grader.py")
_FIX = os.path.join(_HERE, "fixtures")


def _load_grader():
    spec = importlib.util.spec_from_file_location("gt_substitution_grader", _GRADER)
    assert spec and spec.loader, f"grader not found at {_GRADER}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def grader():
    return _load_grader()


def test_reacquired_fixture_flags_one_event(grader):
    """Fixture 1: GT delivers foo@turn2; agent greps foo@turn6 => re_acquisition=1."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_reacq_reacquired"))
    assert rep["trajectory_source"] == "miniswe_trajectory"
    # 'foo' (symbol) + 'src/mod.py' (file) are the two searchable delivered facts.
    assert rep["searchable_delivered_fact_count"] == 2, rep
    assert rep["re_acquisition_event_count"] == 1, rep["re_acquisition_events"]
    # the one re-acquired fact is the foo symbol, searched AFTER it was delivered.
    ev = rep["re_acquisition_events"][0]
    assert ev["fact_token"] == "foo"
    assert ev["search_turn"] > ev["delivered_turn"], ev
    assert rep["re_acquired_fact_count"] == 1
    # non_re_acq = 1 - 1/2 = 0.5
    assert rep["non_re_acquisition_rate"] == 0.5, rep


def test_consumed_fixture_rate_is_one(grader):
    """Fixture 2: GT delivers foo; agent reads src/mod.py directly (no grep) => rate 1.0."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_reacq_consumed"))
    assert rep["re_acquisition_event_count"] == 0, rep["re_acquisition_events"]
    assert rep["re_acquired_fact_count"] == 0
    assert rep["non_re_acquisition_rate"] == 1.0, rep
    # the direct read of the delivered file is NOT a wrong-branch read (it's in facts).
    assert rep["turns_to_first_edit"] == 4, rep


def test_precedence_grep_before_delivery_not_counted(grader):
    """Fixture 3: grep foo@turn1 PRECEDES delivery@turn2 => NOT a re-acquisition."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_reacq_precedence"))
    assert rep["re_acquisition_event_count"] == 0, rep["re_acquisition_events"]
    assert rep["non_re_acquisition_rate"] == 1.0, rep


def test_precedence_mutation_bites(grader):
    """MUTATION: if precedence is ignored (count re-acq even when the grep PRECEDED
    delivery), fixture 3's turn-1 grep is wrongly counted. Prove the guard is load-bearing
    by calling the core with precedence disabled and asserting it now bites."""
    run = grader.load_run(os.path.join(_FIX, "subst_reacq_precedence"))
    # correct (precedence enforced) -> 0
    good = grader.compute_re_acquisition(run["delivered_facts"], run["events"],
                                         enforce_precedence=True)
    assert good["re_acquisition_event_count"] == 0
    # mutant (precedence ignored) -> the pre-delivery grep now counts -> >=1 (BITE)
    mutant = grader.compute_re_acquisition(run["delivered_facts"], run["events"],
                                           enforce_precedence=False)
    assert mutant["re_acquisition_event_count"] >= 1, "mutation did not bite"


def test_determinism_same_input_twice(grader):
    """Same dir graded twice => byte-identical JSON (sorted, no timestamps in payload)."""
    import json
    d = os.path.join(_FIX, "subst_reacq_reacquired")
    a = json.dumps(grader.grade_run(d), sort_keys=True, default=str)
    b = json.dumps(grader.grade_run(d), sort_keys=True, default=str)
    assert a == b


def test_absence_graceful_no_trajectory(grader, tmp_path):
    """A dir with no recognizable trajectory => skipped record, never a crash."""
    rep = grader.grade_run(str(tmp_path))
    assert rep["trajectory_source"] == "none"
    assert rep["non_re_acquisition_rate"] is None
    assert rep["turns_to_first_edit"] is None


# ===========================================================================
# RECEIPT LADDER (W4) — consumption receipt levels 1-4 per fact class.
# Ladder (evidence_envelope.py): 1 DELIVERED · 2 REFERENCED · 3 ACTED ·
# 4 RESOLVED_STATE. Level 5 CAUSAL is paired-only (documented, never computed).
# THE INFLATION LESSON (W2 review): substring matching over-credits receipts
# ('get' in 'target' => false ACTED; 'run' in 'running' => false REFERENCED;
# basename collisions => false ACTED). Detectors MUST use word-boundary
# identifier tokenization + path-aware file comparison. RED-first below.
# ===========================================================================
_FIX_RECEIPT = ("referenced_running", "acted_get_target", "basename_collision",
                "resolved", "m2_further_search", "covering_green",
                "verify_strict_miss", "cdprefix_reacq", "loc_dedup", "oblig_bounds")


def _per_fact(rep_or_receipts, token, cls=None):
    """Locate the receipt_per_fact record for a token (optionally a class)."""
    pf = rep_or_receipts.get("receipt_per_fact") or []
    hits = [f for f in pf if f["token"] == token and (cls is None or f["cls"] == cls)]
    assert hits, f"no receipt_per_fact for token={token!r} cls={cls!r}: {pf}"
    return hits[0]


def test_receipt_referenced_uses_word_boundary_not_substring(grader):
    """FP-CLASS 1 ('run' in 'running'): a later agent message 'the tests are
    running now' must NOT REFERENCE the delivered symbol 'run' (word-boundary)."""
    d = os.path.join(_FIX, "subst_receipt_referenced_running")
    rep = grader.grade_run(d)
    run_fact = _per_fact(rep, "run", "symbol-definition")
    assert run_fact["highest_level"] == "delivered", run_fact
    assert run_fact["levels"]["referenced"] is False, run_fact


def test_receipt_referenced_mutation_substring_bites(grader):
    """M1: revert REFERENCED to substring matching => the 'running' FP bites
    ('run' in 'running' => false REFERENCED). Prove tokenization is load-bearing."""
    run = grader.load_run(os.path.join(_FIX, "subst_receipt_referenced_running"))
    good = grader.compute_receipts(run["delivered_facts"], run["events"],
                                   run["agent_messages"], use_substring_reference=False)
    mutant = grader.compute_receipts(run["delivered_facts"], run["events"],
                                     run["agent_messages"], use_substring_reference=True)
    g = _per_fact(good, "run", "symbol-definition")
    m = _per_fact(mutant, "run", "symbol-definition")
    assert g["levels"]["referenced"] is False, g
    assert m["levels"]["referenced"] is True, "mutation (substring) did not bite"
    assert m["highest_level"] == "referenced"


def test_receipt_acted_get_not_matched_by_target(grader):
    """FP-CLASS 2 ('get' in 'target'): a later action 'cat target_helper.py' must
    NOT count as ACTED on the delivered symbol 'get' (word-boundary tokenization)."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_acted_get_target"))
    get_fact = _per_fact(rep, "get", "symbol-definition")
    assert get_fact["levels"]["acted"] is False, get_fact
    assert get_fact["highest_level"] == "delivered", get_fact


def test_receipt_acted_basename_collision_is_path_aware(grader):
    """FP-CLASS 3 (basename collision): delivering a/util.py AND b/util.py, then
    'cat a/util.py' ACTS on a/util.py ONLY — b/util.py must stay DELIVERED (the
    ambiguous basename must NOT credit the wrong file)."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_basename_collision"))
    a = _per_fact(rep, "a/util.py", "localization-file")
    b = _per_fact(rep, "b/util.py", "localization-file")
    assert a["levels"]["acted"] is True, a
    assert b["levels"]["acted"] is False, b


def test_receipt_resolved_state_edit_lands_and_no_research(grader):
    """L4 positive: delivered def foo -> src/mod.py, then an edit lands in src/mod.py
    with NO further search for foo => RESOLVED_STATE. Also proves L2 (the message
    'editing foo in mod' names foo) fires positively."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_resolved"))
    foo = _per_fact(rep, "foo", "symbol-definition")
    assert foo["levels"]["referenced"] is True, foo
    assert foo["levels"]["acted"] is True, foo
    assert foo["levels"]["resolved_state"] is True, foo
    assert foo["highest_level"] == "resolved_state", foo


def test_receipt_resolved_state_mutation_no_further_search_bites(grader):
    """M2: break level-4's no-further-search condition. When the agent edits
    src/mod.py AND THEN re-greps foo, the fact was NOT substituted => correct code
    withholds RESOLVED_STATE (stays ACTED). Dropping the condition wrongly promotes."""
    run = grader.load_run(os.path.join(_FIX, "subst_receipt_m2_further_search"))
    good = grader.compute_receipts(run["delivered_facts"], run["events"],
                                   run["agent_messages"], enforce_no_further_search=True)
    mutant = grader.compute_receipts(run["delivered_facts"], run["events"],
                                     run["agent_messages"], enforce_no_further_search=False)
    g = _per_fact(good, "foo", "symbol-definition")
    m = _per_fact(mutant, "foo", "symbol-definition")
    assert g["highest_level"] == "acted", g
    assert g["levels"]["resolved_state"] is False, g
    assert m["levels"]["resolved_state"] is True, "mutation (no no-search guard) did not bite"


def test_receipt_verify_nudge_strict_red_to_green(grader):
    """L4 STRICT for verify_nudge_file (renamed from covering-test, F2): delivered
    tests/test_bar.py, then a later test run that NAMES the delivered file goes green
    => RESOLVED_STATE (strict AND weak)."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_covering_green"))
    t = _per_fact(rep, "tests/test_bar.py", "verify_nudge_file")
    assert t["levels"]["resolved_state"] is True, t
    assert t["highest_level"] == "resolved_state", t
    assert t["resolved_state_any_green"] is True, t


def test_receipt_verify_strict_requires_file_identity(grader):
    """F1 RED (the arviz-2413 case): a later GREEN test that never NAMES the delivered
    file (pytest tests/test_plots_matplotlib.py vs delivered arviz/plots/hdiplot.py)
    must NOT promote under STRICT L4; the any-green WEAK column still records it."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_verify_strict_miss"))
    t = _per_fact(rep, "arviz/plots/hdiplot.py", "verify_nudge_file")
    assert t["levels"]["resolved_state"] is False, t          # strict = decision column
    assert t["resolved_state_any_green"] is True, t           # weak column kept
    assert t["highest_level"] != "resolved_state", t
    blk = rep["receipt_by_class"]["verify_nudge_file"]
    assert blk["at_least"]["resolved_state"] == 0, blk
    assert blk["resolved_state_weak_any_green"] == 1, blk


def test_receipt_class_renamed_and_legacy_alias_stable(grader):
    """F2: the class is verify_nudge_file in ALL receipt output; the pre-existing
    delivered_fact_count key keeps the LEGACY name covering-test (baseline-diff law)."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_covering_green"))
    assert "verify_nudge_file" in rep["receipt_by_class"]
    assert "covering-test" not in rep["receipt_by_class"]
    assert rep["delivered_fact_count"]["covering-test"] == 1
    assert "verify_nudge_file" not in rep["delivered_fact_count"]
    assert rep["fact_class_legacy_aliases"] == {"verify_nudge_file": "covering-test"}


def test_cdprefix_grep_counts_reacq_and_holds_L4(grader):
    """F4 RED (the sh-744 case): the parity harness prefixes commands with
    'cd $(cat /tmp/gt_root.txt) && …'. The post-delivery grep behind that prefix MUST
    classify as a search => counts as re-acquisition AND withholds L4 resolved_state."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_cdprefix_reacq"))
    assert rep["search_count"] == 1, rep
    assert rep["re_acquisition_event_count"] == 1, rep["re_acquisition_events"]
    foo = _per_fact(rep, "foo", "symbol-definition")
    assert foo["levels"]["resolved_state"] is False, foo      # L4 guard holds
    assert foo["highest_level"] == "acted", foo


def test_localization_dedup_suffix_groups(grader):
    """F3: localization-file variants (fsm/context.py vs aiogram/fsm/context.py) group
    by suffix identity; group receipt = max over variants. DEDUPED column reported."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_loc_dedup"))
    blk = rep["receipt_by_class"]["localization-file"]
    assert blk["delivered_distinct"] == 3, blk                 # raw: 2 variants + other.py
    ded = blk["deduped"]
    assert ded["group_count"] == 2, ded                        # variants collapse
    assert ded["at_least"]["resolved_state"] == 1, ded         # the edited group
    assert ded["rate"]["resolved_state"] == 0.5, ded
    # non-localization classes carry deduped=None (missing=null convention)
    assert rep["receipt_by_class"]["symbol-definition"]["deduped"] is None


def test_obligation_referenced_bounds_columns(grader):
    """F5: obligation REFERENCED is a LOWER BOUND (entity-anchored). The loose
    any-token ceiling and the code-shaped heuristic middle are reported as columns:
    lower(1) <= code_shaped(2) <= loose_ceiling(3) on the bounds fixture."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_oblig_bounds"))
    blk = rep["receipt_by_class"]["obligation"]
    b = blk["referenced_bounds"]
    assert blk["delivered_distinct"] == 3, blk
    assert b["lower_entity_anchored"] == 1, b                  # validate_schema anchored
    assert b["code_shaped_heuristic"] == 2, b                  # + MyClass (camelCase)
    assert b["loose_ceiling"] == 3, b                          # + docs/stay/accurate
    assert b["lower_entity_anchored"] == blk["at_least"]["referenced"]
    # bounds only exist for the obligation class
    assert rep["receipt_by_class"]["caller-fact"]["referenced_bounds"] is None


def test_receipt_by_class_shape_and_rates(grader):
    """The per-fact-class consumption table: delivered_distinct + highest-level
    distribution + cumulative at_least + 8dp rates; distribution sums to delivered."""
    rep = grader.grade_run(os.path.join(_FIX, "subst_receipt_resolved"))
    rbc = rep["receipt_by_class"]
    for cls, blk in rbc.items():
        dist = blk["highest_level_distribution"]
        assert sum(dist.values()) == blk["delivered_distinct"], (cls, blk)
        # at_least is a monotone non-increasing cumulative of the distribution
        al = blk["at_least"]
        assert al["delivered"] >= al["referenced"] >= al["acted"] >= al["resolved_state"]
        assert al["delivered"] == blk["delivered_distinct"]
        # rates are 8dp fractions of delivered_distinct (or null when denom 0)
        for lvl, r in blk["rate"].items():
            if blk["delivered_distinct"]:
                assert abs(r - al[lvl] / blk["delivered_distinct"]) < 1e-9
            else:
                assert r is None


def test_receipt_determinism_double_run(grader):
    """Same dir graded twice => byte-identical receipt payload."""
    import json
    d = os.path.join(_FIX, "subst_receipt_resolved")
    a = json.dumps(grader.grade_run(d)["receipt_by_class"], sort_keys=True, default=str)
    b = json.dumps(grader.grade_run(d)["receipt_by_class"], sort_keys=True, default=str)
    assert a == b


def test_receipt_aggregate_admit_cut_table(grader):
    """aggregate() rolls up the per-class highest-level distribution across runs
    into the ADMIT/CUT input table (class -> highest receipt level distribution)."""
    reps = [grader.grade_run(os.path.join(_FIX, "subst_receipt_" + n))
            for n in _FIX_RECEIPT]
    agg = grader.aggregate(reps)
    table = agg["receipt_admit_cut_table"]
    assert set(table) <= set(grader.FACT_CLASSES)
    # verify_nudge_file resolved (strict) at least once across the fixture corpus
    assert table["verify_nudge_file"]["resolved_state"] >= 1, table
    # aggregate rates present at 8dp
    assert "receipt_rate" in table["verify_nudge_file"]
    # F1 weak column + F3 deduped + F5 bounds present at the aggregate level
    assert "resolved_state_weak_any_green" in table["verify_nudge_file"]
    assert "deduped" in table["localization-file"]
    assert "referenced_bounds" in table["obligation"]
