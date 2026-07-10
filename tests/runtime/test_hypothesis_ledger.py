"""HypothesisLedger (W4) — recovery reasoning over TYPED state, not text heuristics.

TTD: every test names the invariant it pins. The suite pins:
  * Hypothesis envelope: id determinism, EXACT to_dict/from_dict round trip, tuple ingress.
  * hypothesis_slots ingress: dicts stored (not objects) so EpisodeState's OWN round-trip
    law survives; idempotent add; record_experiment / mark_* ; reset clears the slots.
  * The 6 named transitions, each as a scripted (EpisodeState, LedgerEvent) fixture, with
    the correct-or-quiet abstentions.
  * M1 — the observation-novelty distinction (new-output != loop).
  * M2 — falsification (edit + repeat-fail => hypothesis falsified).
  * Advisory laws (non-blocking, non-directive), serialization, 2-process determinism.
  * classify_all deterministic ordering + empty on a bare event.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from groundtruth.runtime.episode_state import EpisodeState
from groundtruth.runtime.evidence_envelope import ADVISORY, INFO, VERIFIED, WARNING
from groundtruth.runtime.hypothesis_ledger import (
    Advisory,
    D_ALTERNATE_SURFACE_CANDIDATE,
    D_HYPOTHESIS_FALSIFIED,
    D_NOT_A_LOOP,
    D_REPAIR_NOT_SOURCE,
    D_REFRESH_BEFORE_ADVICE,
    D_REQUEST_NEW_HYPOTHESIS,
    Hypothesis,
    LedgerEvent,
    STATUS_FALSIFIED,
    STATUS_OPEN,
    STATUS_SUPERSEDED,
    STATUS_SUPPORTED,
    T_EDIT_CONTRADICTED_CONTRACT,
    T_ENV_FAILURE,
    T_NO_DISCRIMINATING_EVIDENCE,
    T_SAME_COMMAND_NEW_OUTPUT,
    T_SAME_FAILURE_UNCHANGED_PATCH,
    T_STALE_GRAPH,
    add_hypothesis,
    advisory_violations,
    classify_all,
    classify_edit_contradicted_contract,
    classify_env_failure,
    classify_no_discriminating_evidence,
    classify_same_command_new_output,
    classify_same_failure_unchanged_patch,
    classify_stale_graph,
    get_hypothesis,
    hypotheses,
    mark_falsified,
    mark_superseded,
    mark_supported,
    record_experiment,
)


# --------------------------------------------------------------------------- #
# scripted-state fixtures
# --------------------------------------------------------------------------- #
def _state_with_probe(stem: str, outcomes: list[str], indices: list[int]) -> EpisodeState:
    """An EpisodeState whose probe_ledger holds ONE stem with the given outcomes —
    the gt_mini_patch Listen-Lattice ledger shape."""
    e = EpisodeState(episode_id="task-1")
    e.probe_ledger[stem] = {
        "probed_forms": set(),
        "probe_indices": list(indices),
        "outcomes": list(outcomes),
        "answered": None,
    }
    return e


# --------------------------------------------------------------------------- #
# Hypothesis envelope — id determinism + round trip
# --------------------------------------------------------------------------- #
def test_hypothesis_id_is_deterministic_hash_of_claim_and_episode():
    a = Hypothesis.build(claim="the bug is in the loader", episode_id="ep1")
    b = Hypothesis.build(claim="the bug is in the loader", episode_id="ep1")
    c = Hypothesis.build(claim="the bug is in the loader", episode_id="ep2")
    d = Hypothesis.build(claim="a different claim", episode_id="ep1")
    assert a.id == b.id                    # same claim+episode => same id
    assert a.id != c.id                    # episode scopes the id
    assert a.id != d.id                    # claim scopes the id
    assert len(a.id) == 16 and all(ch in "0123456789abcdef" for ch in a.id)
    assert a.status == STATUS_OPEN


def test_id_framing_is_injective_under_embedded_nul():
    """Reviewer attack (id/NUL-boundary-shift): the old '\\x00'-joined framing collided
    ('a', 'b\\x00c') with ('a\\x00b', 'c') — the docstring's no-collision claim was
    FALSE when inputs contain NUL. JSON-list framing is injective on the pair."""
    a = Hypothesis.build(claim="b\x00c", episode_id="a")
    b = Hypothesis.build(claim="c", episode_id="a\x00b")
    assert a.id != b.id


def test_claim_coercion_precedes_id_derivation():
    """Reviewer attack (id/claim-None-inconsistency): build(claim=None) stored claim
    'None' whose id was hashed from '' — the stored claim did not hash to its own id
    (and None silently shared a slot with ''). Coercion must precede derivation."""
    hn = Hypothesis.build(claim=None, episode_id="ep1")  # type: ignore[arg-type]
    assert hn.claim == "None"
    assert hn.id == Hypothesis.build(claim="None", episode_id="ep1").id
    assert hn.id != Hypothesis.build(claim="", episode_id="ep1").id


def test_hypothesis_round_trip_exact_with_tuple_ingress():
    h = Hypothesis.build(
        claim="loader mis-parses empty rows",
        episode_id="ep1",
        supporting_fact_ids=["probe:load:3", "fingerprint:abc"],  # list ingress
        contradicting_fact_ids=("edit:5",),                        # tuple ingress
        predicted_observation="the covering test goes green",
        experiment_command="pytest -k load",
        graph_revision="rev-77",
    )
    d = h.to_dict()
    assert isinstance(d["supporting_fact_ids"], list)   # JSON has no tuple
    h2 = Hypothesis.from_dict(d)
    assert h2 == h                                       # exact object round trip
    assert h2.to_dict() == d
    assert isinstance(h2.supporting_fact_ids, tuple)     # rebuilt as tuple


# --------------------------------------------------------------------------- #
# hypothesis_slots ingress — dicts stored; EpisodeState round-trip survives
# --------------------------------------------------------------------------- #
def test_add_hypothesis_stores_a_dict_slot_and_is_idempotent():
    e = EpisodeState(episode_id="ep1")
    h = Hypothesis.build(claim="X", episode_id="ep1", graph_revision="r1")
    slot = add_hypothesis(e, h)
    assert isinstance(slot, dict)                 # a DICT, not a frozen object
    assert e.hypothesis_slots == [slot]
    # idempotent by id — re-adding the same claim+episode does not duplicate
    again = add_hypothesis(e, Hypothesis.build(claim="X", episode_id="ep1"))
    assert len(e.hypothesis_slots) == 1
    assert again is slot
    assert get_hypothesis(e, h.id) == h
    assert get_hypothesis(e, "nope") is None


def test_hypothesis_slots_preserve_episode_state_round_trip_law():
    """The consumed module's round-trip law (from_dict(to_dict(e)) == e) MUST survive our
    storing hypotheses — which is only true because slots hold JSON-safe DICTS."""
    e = EpisodeState(episode_id="ep1", step_limit=150)
    add_hypothesis(e, Hypothesis.build(claim="A", episode_id="ep1", graph_revision="r1"))
    add_hypothesis(e, Hypothesis.build(claim="B", episode_id="ep1",
                                       supporting_fact_ids=["probe:x:1"]))
    d = e.to_dict()
    json.dumps(d)                                 # must be JSON-serializable
    e2 = EpisodeState.from_dict(d)
    assert e2 == e                                # EpisodeState value equality holds
    assert e2.to_dict() == d
    assert [h.claim for h in hypotheses(e2)] == ["A", "B"]  # insertion order preserved


def test_record_experiment_and_mark_transitions():
    e = EpisodeState(episode_id="ep1")
    h = Hypothesis.build(claim="Y", episode_id="ep1")
    add_hypothesis(e, h)
    assert record_experiment(e, h.id, "test still red") is not None
    assert get_hypothesis(e, h.id).result == "test still red"
    assert mark_supported(e, h.id)["status"] == STATUS_SUPPORTED
    assert mark_falsified(e, h.id)["status"] == STATUS_FALSIFIED
    assert mark_superseded(e, h.id)["status"] == STATUS_SUPERSEDED
    # correct-or-quiet on unknown id
    assert record_experiment(e, "unknown", "x") is None
    assert mark_supported(e, "unknown") is None


def test_reset_attempt_clears_hypothesis_slots():
    """EpisodeState owns reset; we only consume it. After reset the ledger is empty."""
    e = EpisodeState(episode_id="ep1")
    add_hypothesis(e, Hypothesis.build(claim="Z", episode_id="ep1"))
    assert hypotheses(e)
    e.reset_attempt()
    assert e.hypothesis_slots == []
    assert hypotheses(e) == []


# --------------------------------------------------------------------------- #
# TRANSITION 1 — no_discriminating_evidence (all-same outcomes) -> alternate surface
# --------------------------------------------------------------------------- #
def test_no_discriminating_evidence_fires_on_repeated_same_outcome():
    e = _state_with_probe("getuser", ["zero", "zero", "zero"], [3, 6, 9])
    adv = classify_no_discriminating_evidence(e, LedgerEvent(probe_stem="getuser"))
    assert adv is not None
    assert adv.transition == T_NO_DISCRIMINATING_EVIDENCE
    assert adv.disposition == D_ALTERNATE_SURFACE_CANDIDATE
    assert adv.evidence_ids == ("probe:getuser:3", "probe:getuser:6", "probe:getuser:9")
    assert advisory_violations(adv) == []


def test_no_discriminating_evidence_quiet_on_single_probe_and_on_delta():
    single = _state_with_probe("foo", ["zero"], [1])
    assert classify_no_discriminating_evidence(single, LedgerEvent(probe_stem="foo")) is None
    delta = _state_with_probe("foo", ["zero", "hit"], [1, 4])
    assert classify_no_discriminating_evidence(delta, LedgerEvent(probe_stem="foo")) is None
    # unknown stem / empty stem -> quiet
    assert classify_no_discriminating_evidence(single, LedgerEvent(probe_stem="bar")) is None
    assert classify_no_discriminating_evidence(single, LedgerEvent()) is None


# --------------------------------------------------------------------------- #
# TRANSITION 4 + M1 — same_command_new_output: novelty != loop
# --------------------------------------------------------------------------- #
def test_m1_same_command_new_output_is_not_a_loop():
    """M1 target — the observation-novelty distinction. An outcome DELTA on the same probe
    stem is NEW output (progress), NOT a loop. The two novelty classifiers are exact
    complements: on a delta, same_command_new_output FIRES (not_a_loop) and
    no_discriminating_evidence is SILENT. A mutation that treats new-output as a loop
    (e.g. flips either guard) breaks at least one of these two assertions."""
    e = _state_with_probe("parse", ["zero", "hit"], [2, 7])
    ev = LedgerEvent(probe_stem="parse")
    novel = classify_same_command_new_output(e, ev)
    assert novel is not None
    assert novel.transition == T_SAME_COMMAND_NEW_OUTPUT
    assert novel.disposition == D_NOT_A_LOOP           # explicitly NOT a loop
    assert novel.evidence_ids == ("probe:parse:2", "probe:parse:7")
    assert advisory_violations(novel) == []
    # complement: the "stuck" classifier must stay SILENT when there is novelty
    assert classify_no_discriminating_evidence(e, ev) is None


def test_same_command_new_output_quiet_when_all_outcomes_identical():
    e = _state_with_probe("parse", ["hit", "hit"], [2, 7])
    assert classify_same_command_new_output(e, LedgerEvent(probe_stem="parse")) is None
    # and the complement fires there instead
    assert classify_no_discriminating_evidence(e, LedgerEvent(probe_stem="parse")) is not None


# --------------------------------------------------------------------------- #
# TRANSITION 2 + M2 — edit_contradicted_contract: edit + repeat-fail => falsified
# --------------------------------------------------------------------------- #
def _edit_then_repeat_fail_state() -> EpisodeState:
    e = EpisodeState(episode_id="ep1")
    e.failure_fingerprints.add("fp1")                       # fp1 seen before (prior fail)
    e.last_failure_record = {"failure_fingerprint": "fp1", "action_index": 3}
    e.edit_events.append({"index": 5, "blob": "def foo(): return 2"})  # edit AFTER prior fail
    return e


def test_m2_edit_then_repeat_failure_falsifies_hypothesis():
    """M2 target — falsification. An edit (action 5) between the prior occurrence of a
    failure fingerprint (action 3) and its recurrence (action 7) means the edit did NOT
    change the failing result => the hypothesis it embodied is FALSIFIED. A mutation that
    breaks the edit-between detection (edit+fail no longer falsifies) makes this None and
    the assertion bites."""
    e = _edit_then_repeat_fail_state()
    ev = LedgerEvent(failure_fingerprint="fp1", action_index=7)
    adv = classify_edit_contradicted_contract(e, ev)
    assert adv is not None
    assert adv.transition == T_EDIT_CONTRADICTED_CONTRACT
    assert adv.disposition == D_HYPOTHESIS_FALSIFIED
    assert "fingerprint:fp1" in adv.evidence_ids
    assert "edit:5" in adv.evidence_ids
    assert advisory_violations(adv) == []
    # mutual exclusivity: same_failure_unchanged must NOT fire when an edit intervened
    assert classify_same_failure_unchanged_patch(e, ev) is None


def test_edit_contradicted_quiet_on_fresh_fingerprint():
    """A FRESH failure (not yet in failure_fingerprints) is not a contradiction."""
    e = _edit_then_repeat_fail_state()
    ev = LedgerEvent(failure_fingerprint="brand-new", action_index=7)
    assert classify_edit_contradicted_contract(e, ev) is None


# --------------------------------------------------------------------------- #
# TRANSITION 3 — same_failure_unchanged_patch: repeat + no edit => new hypothesis
# --------------------------------------------------------------------------- #
def test_same_failure_unchanged_patch_fires_with_no_edit_between():
    e = EpisodeState(episode_id="ep1")
    e.failure_fingerprints.add("fp2")
    e.last_failure_record = {"failure_fingerprint": "fp2", "action_index": 4}
    # NO edit events at all => nothing changed between the two failures
    ev = LedgerEvent(failure_fingerprint="fp2", action_index=9)
    adv = classify_same_failure_unchanged_patch(e, ev)
    assert adv is not None
    assert adv.transition == T_SAME_FAILURE_UNCHANGED_PATCH
    assert adv.disposition == D_REQUEST_NEW_HYPOTHESIS
    assert adv.evidence_ids == ("fingerprint:fp2",)
    assert advisory_violations(adv) == []
    # complement: edit_contradicted must stay silent (no edit intervened)
    assert classify_edit_contradicted_contract(e, ev) is None


def test_same_failure_unchanged_quiet_on_fresh_fingerprint():
    e = EpisodeState(episode_id="ep1")
    ev = LedgerEvent(failure_fingerprint="fp-never-seen", action_index=9)
    assert classify_same_failure_unchanged_patch(e, ev) is None


# --------------------------------------------------------------------------- #
# TRANSITION 5 — env_failure: imported classifier, repair-not-source
# --------------------------------------------------------------------------- #
def test_env_failure_fires_on_imported_signature():
    e = EpisodeState(episode_id="ep1")
    ev = LedgerEvent(
        observation="ModuleNotFoundError: No module named 'foo'; try pip install foo",
        action_index=12,
    )
    adv = classify_env_failure(e, ev)
    assert adv is not None
    assert adv.transition == T_ENV_FAILURE
    assert adv.disposition == D_REPAIR_NOT_SOURCE
    assert any(ei.startswith("failure_kind:") for ei in adv.evidence_ids)
    assert advisory_violations(adv) == []


def test_env_failure_quiet_on_ordinary_assertion_output():
    e = EpisodeState(episode_id="ep1")
    ev = LedgerEvent(observation="AssertionError: 3 != 4\nexit code 1", action_index=12)
    assert classify_env_failure(e, ev) is None
    assert classify_env_failure(e, LedgerEvent()) is None


# --------------------------------------------------------------------------- #
# TRANSITION 6 — stale_graph: event revision != active hypothesis revision
# --------------------------------------------------------------------------- #
def test_stale_graph_fires_when_revision_moved_since_hypothesis():
    e = EpisodeState(episode_id="ep1")
    add_hypothesis(e, Hypothesis.build(claim="loader bug", episode_id="ep1",
                                       graph_revision="rev-A"))
    adv = classify_stale_graph(e, LedgerEvent(graph_revision="rev-B"))
    assert adv is not None
    assert adv.transition == T_STALE_GRAPH
    assert adv.disposition == D_REFRESH_BEFORE_ADVICE
    assert "graph_revision:rev-A" in adv.evidence_ids
    assert "graph_revision:rev-B" in adv.evidence_ids
    assert advisory_violations(adv) == []


def test_stale_graph_quiet_when_matched_or_ungrounded():
    e = EpisodeState(episode_id="ep1")
    # no hypothesis with a revision -> quiet
    assert classify_stale_graph(e, LedgerEvent(graph_revision="rev-B")) is None
    add_hypothesis(e, Hypothesis.build(claim="c", episode_id="ep1", graph_revision="rev-A"))
    # matching revision -> quiet
    assert classify_stale_graph(e, LedgerEvent(graph_revision="rev-A")) is None
    # event carries no revision -> quiet
    assert classify_stale_graph(e, LedgerEvent()) is None


# --------------------------------------------------------------------------- #
# Advisory laws — non-blocking, non-directive, WARNING/INFO only
# --------------------------------------------------------------------------- #
def test_every_transition_emits_a_law_abiding_advisory():
    """No advisory is ever blocking, VERIFIED-tier, or a directive statement."""
    advisories = [
        classify_no_discriminating_evidence(
            _state_with_probe("s", ["zero", "zero"], [1, 2]), LedgerEvent(probe_stem="s")),
        classify_same_command_new_output(
            _state_with_probe("s", ["zero", "hit"], [1, 2]), LedgerEvent(probe_stem="s")),
        classify_edit_contradicted_contract(
            _edit_then_repeat_fail_state(),
            LedgerEvent(failure_fingerprint="fp1", action_index=7)),
        classify_same_failure_unchanged_patch(
            _state_with_fp("fp2", 4), LedgerEvent(failure_fingerprint="fp2", action_index=9)),
        classify_env_failure(
            EpisodeState(), LedgerEvent(observation="command not found: gcc")),
        classify_stale_graph(_state_with_hyp_rev("rev-A"), LedgerEvent(graph_revision="rev-B")),
    ]
    assert all(a is not None for a in advisories)
    for a in advisories:
        assert advisory_violations(a) == []
        assert a.blocking_eligibility == ADVISORY   # never enforced
        assert a.tier in (WARNING, INFO)            # never VERIFIED (unmeasured)


def test_advisory_violations_rejects_blocking_verified_and_directive():
    ok = Advisory(transition=T_ENV_FAILURE, disposition=D_REPAIR_NOT_SOURCE, tier=INFO,
                  statement="the observation is environmental.", evidence_ids=())
    assert advisory_violations(ok) == []
    blocking = Advisory(transition=T_ENV_FAILURE, disposition=D_REPAIR_NOT_SOURCE, tier=INFO,
                        blocking_eligibility="blocking", statement="the observation is env.")
    assert any("advisory" in v for v in advisory_violations(blocking))
    verified = Advisory(transition=T_ENV_FAILURE, disposition=D_REPAIR_NOT_SOURCE,
                        tier=VERIFIED, statement="the observation is env.")
    assert any("VERIFIED" in v for v in advisory_violations(verified))
    directive = Advisory(transition=T_ENV_FAILURE, disposition=D_REPAIR_NOT_SOURCE, tier=INFO,
                         statement="edit the loader to fix it")
    assert any("imperative" in v for v in advisory_violations(directive))
    unknown = Advisory(transition="made_up", disposition=D_REPAIR_NOT_SOURCE, tier=INFO,
                       statement="a fact.")
    assert any("transition" in v for v in advisory_violations(unknown))


def _state_with_fp(fp: str, prior_idx: int) -> EpisodeState:
    e = EpisodeState(episode_id="ep1")
    e.failure_fingerprints.add(fp)
    e.last_failure_record = {"failure_fingerprint": fp, "action_index": prior_idx}
    return e


def _state_with_hyp_rev(rev: str) -> EpisodeState:
    e = EpisodeState(episode_id="ep1")
    add_hypothesis(e, Hypothesis.build(claim="c", episode_id="ep1", graph_revision=rev))
    return e


# --------------------------------------------------------------------------- #
# Advisory serialization round trip
# --------------------------------------------------------------------------- #
def test_advisory_round_trip_exact():
    a = classify_no_discriminating_evidence(
        _state_with_probe("s", ["zero", "zero"], [1, 2]), LedgerEvent(probe_stem="s"))
    d = a.to_dict()
    assert isinstance(d["evidence_ids"], list)
    assert Advisory.from_dict(d) == a
    assert Advisory.from_dict(d).to_dict() == d


# --------------------------------------------------------------------------- #
# classify_all — deterministic ordering + empty on a bare event
# --------------------------------------------------------------------------- #
def test_classify_all_empty_on_bare_event():
    assert classify_all(EpisodeState(), LedgerEvent()) == []


def test_classify_all_deterministic_order_and_multi_fire():
    """A single event can satisfy independent evidence for >1 transition; classify_all
    returns them in the FIXED TRANSITIONS order (env, stale, edit-contradicted,
    same-failure, no-discriminating, same-command)."""
    e = _edit_then_repeat_fail_state()                       # edit + repeat fail (fp1)
    add_hypothesis(e, Hypothesis.build(claim="c", episode_id="ep1", graph_revision="rev-A"))
    e.probe_ledger["s"] = {"probed_forms": set(), "probe_indices": [1, 2],
                           "outcomes": ["zero", "zero"], "answered": None}
    ev = LedgerEvent(
        failure_fingerprint="fp1", action_index=7, graph_revision="rev-B",
        observation="command not found: gcc", probe_stem="s",
    )
    names = [a.transition for a in classify_all(e, ev)]
    assert names == [
        T_ENV_FAILURE,
        T_STALE_GRAPH,
        T_EDIT_CONTRADICTED_CONTRACT,
        T_NO_DISCRIMINATING_EVIDENCE,
    ]
    # run twice — identical order (no set iteration in the path)
    assert [a.transition for a in classify_all(e, ev)] == names


# --------------------------------------------------------------------------- #
# F1 (Fable bounce 2026-07-10) — degraded prior-index path must NEVER falsify.
# The mini seam writes no last_failure_record index by default, so prior=-1 is the
# DEFAULT reachability there; "any edit ever before now" is NOT "edit between the
# two occurrences".
# --------------------------------------------------------------------------- #
def test_f1_edit_before_first_occurrence_never_falsifies():
    """F1 RED case 1 (reviewer repro): the only edit (action 1) happened BEFORE the
    fingerprint's first occurrence (action 3 — index NOT recorded, no tolerant key);
    the recurrence at action 100 had NO intervening edit. hypothesis_falsified here is
    a FALSE FALSIFICATION; with the ordering unprovable BOTH complements must abstain
    (correct-or-quiet)."""
    e = EpisodeState(episode_id="ep1")
    e.failure_fingerprints.add("fp1")
    e.last_failure_record = {"failure_fingerprint": "fp1", "when": "earlier"}
    e.edit_events.append({"index": 1, "blob": "x"})
    ev = LedgerEvent(failure_fingerprint="fp1", action_index=100)
    assert classify_edit_contradicted_contract(e, ev) is None
    assert classify_same_failure_unchanged_patch(e, ev) is None


def test_f1_all_indices_unknown_no_falsification_no_sentinel():
    """F1 RED case 2 (reviewer repro): prior index unknown AND event index unknown
    (-1 default), one old edit. No falsification may be emitted on ZERO ordering
    evidence, and the -1 sentinel must NEVER be interpolated into an agent-facing
    statement."""
    e = EpisodeState(episode_id="ep1")
    e.failure_fingerprints.add("fp1")
    e.last_failure_record = {"failure_fingerprint": "fp1"}
    e.edit_events.append({"index": 5, "blob": "x"})
    ev = LedgerEvent(failure_fingerprint="fp1")  # action_index = -1
    assert classify_edit_contradicted_contract(e, ev) is None
    for adv in classify_all(e, ev):
        assert "-1" not in adv.statement


def test_f1_other_fingerprint_record_not_borrowed_abstains():
    """F1 RED case 3 (reviewer repro): last_failure_record documents a DIFFERENT
    fingerprint, so its index is not borrowed (prior unknown); an edit exists whose
    ordering vs fpB's first occurrence is unprovable -> the falsifier abstains."""
    e = EpisodeState(episode_id="ep1")
    e.failure_fingerprints.add("fpB")
    e.last_failure_record = {"failure_fingerprint": "fpA", "action_index": 8}
    e.edit_events.append({"index": 2, "blob": "x"})
    ev = LedgerEvent(failure_fingerprint="fpB", action_index=10)
    assert classify_edit_contradicted_contract(e, ev) is None


def test_f1_unknown_prior_with_zero_edits_still_requests_new_hypothesis():
    """The DECIDABLE half survives F1: with ZERO edits recorded, 'no edit intervened'
    is provable regardless of the unknown prior index, so same_failure_unchanged_patch
    still fires (the recovery value is not thrown away with the bathwater)."""
    e = EpisodeState(episode_id="ep1")
    e.failure_fingerprints.add("fp1")
    e.last_failure_record = {"failure_fingerprint": "fp1"}  # no index key
    ev = LedgerEvent(failure_fingerprint="fp1", action_index=9)
    adv = classify_same_failure_unchanged_patch(e, ev)
    assert adv is not None
    assert adv.disposition == D_REQUEST_NEW_HYPOTHESIS
    assert classify_edit_contradicted_contract(e, ev) is None


# --------------------------------------------------------------------------- #
# F2 (Fable bounce) — a malformed probe_ledger entry must ABSTAIN, never crash
# classify_all (crash-not-quiet kills the whole recovery layer).
# --------------------------------------------------------------------------- #
def _raw_probe_state(stem: str, outcomes, indices) -> EpisodeState:
    """Fixture that stores probe-ledger values VERBATIM (no coercion) — the malformed
    shapes a buggy seam could write."""
    e = EpisodeState(episode_id="task-1")
    e.probe_ledger[stem] = {"probed_forms": set(), "probe_indices": indices,
                            "outcomes": outcomes, "answered": None}
    return e


def test_f2_unhashable_outcome_member_abstains_not_crashes():
    """F2 RED (reviewer repro): outcomes containing LISTS (unhashable) TypeError'd out
    of set(outcomes) and killed classify_all. Non-conforming members => abstain."""
    e = _raw_probe_state("s", [["zero"], ["zero"]], [1, 2])
    ev = LedgerEvent(probe_stem="s")
    assert classify_no_discriminating_evidence(e, ev) is None
    assert classify_same_command_new_output(e, ev) is None
    assert classify_all(e, ev) == []          # the whole layer survives


def test_f2_none_outcomes_abstains_not_crashes():
    """F2 RED (reviewer repro): 'outcomes': None crashed list(None)."""
    e = _raw_probe_state("s", None, [1])
    ev = LedgerEvent(probe_stem="s")
    assert classify_no_discriminating_evidence(e, ev) is None
    assert classify_same_command_new_output(e, ev) is None
    assert classify_all(e, ev) == []


def test_f2_scalar_string_outcomes_is_one_probe_not_char_split():
    """F2/P5 RED (reviewer repro): 'outcomes': 'zero' (seam misuse) char-split into
    ['z','e','r','o'] and fired a FALSE not_a_loop. A bare str is ONE outcome."""
    e = _raw_probe_state("s", "zero", [1])
    ev = LedgerEvent(probe_stem="s")
    assert classify_same_command_new_output(e, ev) is None    # 1 probe != a delta
    assert classify_no_discriminating_evidence(e, ev) is None  # 1 probe != stuck


def test_f2_non_dict_entry_abstains_and_classify_all_survives_poison():
    """F2 RED: a non-dict ledger entry AttributeError'd; and one poisoned stem must
    never mute a well-formed one (per-classifier isolation in classify_all)."""
    e = _raw_probe_state("s", ["zero", "zero"], [1, 2])
    e.probe_ledger["poison"] = "not-a-dict"
    assert classify_no_discriminating_evidence(e, LedgerEvent(probe_stem="poison")) is None
    advs = classify_all(e, LedgerEvent(probe_stem="s"))
    assert [a.transition for a in advs] == [T_NO_DISCRIMINATING_EVIDENCE]


# --------------------------------------------------------------------------- #
# F3 (Fable bounce) — advisory teeth: the noun-phrase FACT form must pass; polite /
# second-person-modal / additional bare-verb imperatives must be rejected.
# --------------------------------------------------------------------------- #
def test_f3_noun_phrase_label_is_not_a_directive():
    """F3 RED false-reject (reviewer repro): 'Edit target: ...' is a label-headed
    noun-phrase FACT, not an imperative — must NOT be rejected."""
    a = Advisory(transition=T_ENV_FAILURE, disposition=D_REPAIR_NOT_SOURCE, tier=INFO,
                 statement="Edit target: the loader module.")
    assert advisory_violations(a) == []


def test_f3_polite_modal_and_bare_verb_directives_are_rejected():
    """F3 RED misses (reviewer repro): politeness-prefixed, second-person-modal, and
    additional bare-verb imperatives must all be flagged as directives."""
    for stmt in [
        "Please edit the loader.",
        "You should edit the loader.",
        "Rerun the failing test.",
        "Install the missing package.",
        "Ensure the loader returns None.",
        "Apply this patch.",
        "Grep for the symbol.",
    ]:
        a = Advisory(transition=T_ENV_FAILURE, disposition=D_REPAIR_NOT_SOURCE,
                     tier=INFO, statement=stmt)
        assert any("imperative" in v for v in advisory_violations(a)), stmt


# --------------------------------------------------------------------------- #
# F4 (Fable bounce) — slot-dict aliasing: the exported snapshot and an in-process
# round-tripped copy must be ISOLATED from later mark_* mutations.
# --------------------------------------------------------------------------- #
def test_f4_to_dict_snapshot_is_isolated_from_later_marks():
    """F4 RED (reviewer repro): to_dict shared live slot dicts (list(...) vs
    edit_events' per-item dict(ev)); mark_supported rewrote the already-exported
    snapshot."""
    e = EpisodeState(episode_id="ep1")
    h = Hypothesis.build(claim="rt", episode_id="ep1")
    add_hypothesis(e, h)
    d = e.to_dict()
    mark_supported(e, h.id)
    assert d["hypothesis_slots"][0]["status"] == STATUS_OPEN   # snapshot unchanged
    assert get_hypothesis(e, h.id).status == STATUS_SUPPORTED  # live state changed


def test_f4_round_tripped_copy_is_isolated_from_original():
    """F4 RED (reviewer repro): mark_falsified on the IN-PROCESS round-tripped copy
    must never rewrite the original's slot (no shared dicts through to_dict/from_dict)."""
    e = EpisodeState(episode_id="ep1")
    h = Hypothesis.build(claim="rt2", episode_id="ep1")
    add_hypothesis(e, h)
    e2 = EpisodeState.from_dict(e.to_dict())
    mark_falsified(e2, h.id)
    assert get_hypothesis(e, h.id).status == STATUS_OPEN
    assert get_hypothesis(e2, h.id).status == STATUS_FALSIFIED


# --------------------------------------------------------------------------- #
# DETERMINISM — 2-process byte-identity under different PYTHONHASHSEED
# --------------------------------------------------------------------------- #
def _repo_src() -> str:
    return str(Path(__file__).resolve().parents[2] / "src")


def test_determinism_two_processes(tmp_path):
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {json.dumps(_repo_src())})\n"
        "from groundtruth.runtime.episode_state import EpisodeState\n"
        "from groundtruth.runtime.hypothesis_ledger import (\n"
        "    Hypothesis, LedgerEvent, add_hypothesis, classify_all)\n"
        "e = EpisodeState(episode_id='ep1')\n"
        "for c, r in [('claim-z','rev-z'), ('claim-a','rev-a'), ('claim-m','rev-m')]:\n"
        "    add_hypothesis(e, Hypothesis.build(claim=c, episode_id='ep1',\n"
        "        supporting_fact_ids=['probe:%s:1' % c], graph_revision=r))\n"
        "e.failure_fingerprints.add('fpX')\n"
        "e.last_failure_record = {'failure_fingerprint': 'fpX', 'action_index': 2}\n"
        "e.edit_events.append({'index': 5, 'blob': 'def foo(): return 2'})\n"
        "e.probe_ledger['stm'] = {'probed_forms': set(), 'probe_indices': [1, 2],\n"
        "    'outcomes': ['zero', 'zero'], 'answered': None}\n"
        "ev = LedgerEvent(failure_fingerprint='fpX', action_index=8,\n"
        "    graph_revision='rev-live', observation='command not found', probe_stem='stm')\n"
        "advs = [a.to_dict() for a in classify_all(e, ev)]\n"
        "print(json.dumps({'state': e.to_dict(), 'advisories': advs}, sort_keys=True))\n"
    )
    env0 = {**os.environ, "PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"}
    env1 = {**os.environ, "PYTHONHASHSEED": "1", "PYTHONIOENCODING": "utf-8"}
    r0 = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, env=env0)
    r1 = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, env=env1)
    assert r0.returncode == 0, r0.stderr
    assert r1.returncode == 0, r1.stderr
    assert r0.stdout == r1.stdout
    payload = json.loads(r0.stdout)
    assert [a["transition"] for a in payload["advisories"]] == [
        T_ENV_FAILURE, T_STALE_GRAPH, T_EDIT_CONTRADICTED_CONTRACT,
        T_NO_DISCRIMINATING_EVIDENCE,
    ]
