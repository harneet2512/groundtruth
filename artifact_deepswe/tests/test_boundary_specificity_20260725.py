"""#29 — deliver the fact that answers THIS observation, not the highest-ranked one.

THE CONTRADICTION THIS RESOLVES. `_EVIDENCE_TYPE_RANK` is a STATIC global table (covering_verdict 60
... missing_role 22, new_file_destination 15). Under one global dose, `newfile_precedent` can never
win an observation where a caller/signature fact also exists (48/50) — so it can never reach
`delivered_byte_proven`, and can never be SS-LIVE however correct and timely it is. "All 17 SS-LIVE"
and "<=1 dose with a static global ranking" are mutually exclusive by construction.

Worse, delivering the HIGHEST-RANKED rather than the MOST-RELEVANT fact is itself a
`correct_rl_adhered_time` violation — that gate means the fact that answers THIS observation.

WHY LEXICOGRAPHIC, NOT AN ADDITIVE BOOST. Rank gaps are small and covering-RED leads the next class
by only 10. A boost big enough to let missing_role (22) beat caller_break (48) would need 26+, which
would also let an advisory out-dose an EXECUTED world-fact — exactly what `_xsession_boost` is
capped to prevent. A separate LEADING tuple element avoids the conflict: matched sorts above
unmatched, and severity still orders WITHIN the matched set.

NOT INVENTED POLICY: the boundary comes from `fact_registry.required_event()`, a contract written and
reviewed independently of any benchmark. That is what keeps this out of hand-tuned territory.
"""
from __future__ import annotations
import pytest

from groundtruth.runtime.adapters.miniswe import _boundary_match, _priority, arbitrate
from groundtruth.runtime.fact_registry import required_event


class _Env:
    """Minimal envelope stand-in — _priority reads only these fields."""
    def __init__(self, evidence_type, tier="VERIFIED", confidence=1.0, key=None, native_args=None):
        self.evidence_type = evidence_type
        self.tier = tier
        self.confidence = confidence
        self.dedup_key = key or evidence_type
        self.native_args = native_args or {}


def test_off_by_default_is_byte_identical():
    """No observed_event => the leading term is constant 0 => today's order exactly."""
    envs = [_Env("new_file_destination"), _Env("caller_break"), _Env("covering_verdict")]
    assert all(_priority(e)[0] == 0 for e in envs)
    assert arbitrate(envs).evidence_type == "covering_verdict"


def test_the_contracted_fact_wins_its_own_boundary():
    """THE FIX: on a failed_search, newfile_precedent beats a higher-ranked caller fact."""
    nfd, caller = _Env("new_file_destination"), _Env("caller_break")
    assert required_event("new_file_destination") == "failed_search"
    assert arbitrate([nfd, caller], frozenset(), "failed_search").evidence_type == \
        "new_file_destination", "the fact contracted for this observation still lost the dose"


def test_static_table_would_have_lost_it():
    """Guards the premise: without the boundary, the static rank sends the dose elsewhere."""
    assert arbitrate([_Env("new_file_destination"), _Env("caller_break")]).evidence_type == \
        "caller_break"


def test_executed_world_fact_is_never_out_dosed_by_an_advisory():
    """The cap that matters. On a test_result BOTH match, so severity decides and
    covering_verdict (60) keeps its dominance — a boost-based design could not guarantee this."""
    assert required_event("covering_verdict") == "test_result"
    envs = [_Env("covering_verdict"), _Env("body_concept")]
    assert arbitrate(envs, frozenset(), "test_result").evidence_type == "covering_verdict"


def test_severity_still_orders_within_the_matched_set():
    """Specificity is a FILTER, not a re-ranking: among facts contracted for the same boundary the
    existing severity order is untouched (signature_mismatch 50 > caller_break 48)."""
    assert required_event("signature_mismatch") == required_event("caller_break") == "edit_result"
    envs = [_Env("caller_break"), _Env("signature_mismatch")]
    assert arbitrate(envs, frozenset(), "edit_result").evidence_type == "signature_mismatch"


def test_unregistered_type_ranks_exactly_as_today():
    """Correct-or-quiet: an unresolvable evidence_type scores 0 and competes as it always has."""
    assert _boundary_match(_Env("not_a_registered_type"), "edit_result") == 0


def test_selection_is_a_pure_function_of_its_inputs():
    """Determinism precondition — the same inputs must give the same winner every time, or no
    offline proof transfers to an online run (ss_gate flaked 4/6 RED on exactly this class)."""
    envs = [_Env("new_file_destination"), _Env("caller_break"), _Env("covering_verdict")]
    winners = {arbitrate(list(envs), frozenset(), "failed_search").evidence_type for _ in range(50)}
    assert len(winners) == 1, f"non-deterministic winner: {winners}"


# ---------------------------------------------------------------------------
# boundary_for_event — deriving the §1 boundary from the observation.
# The §1 vocabulary is FINER than the event kind, and the difference is
# load-bearing: newfile_precedent is contracted to `failed_search`, NOT
# `search_result`. A search that returned hits and one that came back EMPTY are
# different decision moments, and only the empty one owes a new-file precedent.
# ---------------------------------------------------------------------------
from groundtruth.runtime.adapters.miniswe import boundary_for_event
from groundtruth.runtime.gateway import (
    KIND_EDIT, KIND_SEARCH, KIND_TEST, KIND_VIEW, ToolEvent)


@pytest.mark.parametrize("kind,zero,expected", [
    (KIND_EDIT, False, "edit_result"),
    (KIND_TEST, False, "test_result"),
    (KIND_VIEW, False, "file_view"),
    (KIND_SEARCH, False, "search_result"),
    (KIND_SEARCH, True, "failed_search"),
])
def test_boundary_derivation(kind, zero, expected):
    assert boundary_for_event(ToolEvent(kind=kind), zero_results=zero) == expected


def test_empty_search_is_a_different_boundary_than_a_hit():
    """THE distinction newfile_precedent depends on — collapsing these would silently
    re-break the feature this whole change exists to unblock."""
    ev = ToolEvent(kind=KIND_SEARCH)
    assert boundary_for_event(ev, zero_results=True) != boundary_for_event(ev, zero_results=False)
    assert required_event("new_file_destination") == boundary_for_event(ev, zero_results=True)


def test_undeterminable_boundary_ranks_as_today():
    """Correct-or-quiet: an unknown boundary must never invent a match."""
    assert boundary_for_event(None) is None
    assert boundary_for_event(ToolEvent(kind="something_else")) is None


# ---------------------------------------------------------------------------
# #28 DRIFT RESOLUTION — executed world-facts are BOUNDARY-EXEMPT.
#
# The registry contracts covering_red to `test_result`, but the producer fires
# post-EDIT (and deliberately steps aside when the agent JUST ran a test). With
# specificity live, a naive boundary match scores it 0 on an edit turn and lets a
# caller/signature ADVISORY out-dose the repo's own executed RED — the exact
# inversion the lexicographic design was chosen to prevent. This was a real
# regression introduced by #29 and caught by the drift audit.
#
# The resolution is NOT a new carve-out: `_filter_candidates_by_phase` already
# exempts the same class from the phase gate, calling it "a verified WORLD-FACT
# ... valid in ANY phase". Boundary exemption applies the established rule.
# ---------------------------------------------------------------------------
from groundtruth.runtime.adapters.miniswe import _WORLD_FACT_EVIDENCE_TYPES


def test_executed_covering_red_is_not_out_dosed_on_an_edit_turn():
    """THE regression. covering_red is contracted to test_result but produced post-EDIT."""
    assert required_event("covering_verdict") == "test_result"
    winner = arbitrate([_Env("covering_verdict"), _Env("caller_break")],
                       frozenset(), "edit_result")
    assert winner.evidence_type == "covering_verdict", (
        "an executed world-fact lost the dose to an advisory because its registered boundary "
        "is not the observation it is produced on — the inversion #29 must never cause"
    )


def test_world_fact_exemption_is_an_explicit_set_not_a_heuristic():
    """Adding one must be a deliberate, reviewable act — never inferred from a name."""
    assert "covering_verdict" in _WORLD_FACT_EVIDENCE_TYPES
    assert isinstance(_WORLD_FACT_EVIDENCE_TYPES, frozenset)
    assert "caller_break" not in _WORLD_FACT_EVIDENCE_TYPES, \
        "an advisory must never be exempted — that would restore the static-table behaviour"


def test_exemption_does_not_break_contracted_facts_winning_their_boundary():
    """The exemption must not swallow the fix it protects: on a failed_search with no world-fact
    present, newfile_precedent still wins."""
    assert arbitrate([_Env("new_file_destination"), _Env("caller_break")],
                     frozenset(), "failed_search").evidence_type == "new_file_destination"


# ---------------------------------------------------------------------------
# #31 — CAP byte-owners resolve through TWO contract tables.
#
# 5 of the 7 CAP byte-owners already resolve via fact_registry through the
# evidence_type whose bytes they own (GT_EDIT_CHECK->edit_result,
# GT_PATCH_DELTA->edit_result, GT_SS_SUBMIT_RED->submit,
# GT_CHANGE_SURFACE->failed_search, GT_LOC_RESLOT->search_result). No new
# registrations were needed — the ALREADY_BUILT check paid again.
#
# The remaining two are STEERS, and steers declare their boundary in
# context_policy.EVENT_BOUND_PAYLOADS, not fact_registry. Consulting only the
# fact registry scored every steer 0, so a contracted FACT would always out-rank
# a steer that WAS contracted for this very observation.
# ---------------------------------------------------------------------------

def test_steer_matches_its_event_bound_boundary():
    """GT_HYPOTHESIS owns verify.horizon.pivot, event-bound to TEST_RESULT."""
    assert _boundary_match(_Env("verify.horizon.pivot"), "test_result") == 1


def test_steer_does_not_match_an_unrelated_boundary():
    """The binding must still DISCRIMINATE — matching everywhere is the static table again."""
    assert _boundary_match(_Env("verify.horizon.pivot"), "search_result") == 0


def test_review_transition_steers_bind_there_not_to_test_result():
    for et in ("verify.horizon.advisory", "verify.horizon.urgent"):
        assert _boundary_match(_Env(et), "review_transition") == 1, et
        assert _boundary_match(_Env(et), "test_result") == 0, et


def test_fact_table_still_wins_where_it_resolves():
    """Fact resolution is consulted FIRST; the steer table is a fallback, not an override."""
    assert _boundary_match(_Env("new_file_destination"), "failed_search") == 1
    assert _boundary_match(_Env("new_file_destination"), "test_result") == 0


# ---------------------------------------------------------------------------
# #32 — the EXPIRY gate needs THREE states, not two.
# `_boundary_match` collapses to 0/1 because RANKING only asks "is this the
# contracted fact". An expiry gate cannot: 0 conflates "contracted elsewhere =>
# expired" with "no contract found => we know nothing". Blocking the second
# would DELETE evidence on an unknown — strictly worse than the ranking bugs
# #29 already produced, because a ranking bug misorders and an expiry bug erases.
# ---------------------------------------------------------------------------
from groundtruth.runtime.adapters.miniswe import (
    BOUNDARY_MATCH, BOUNDARY_MISMATCH, BOUNDARY_UNKNOWN, boundary_verdict)


def test_unknown_never_blocks():
    """THE safety property. No resolvable contract => deliver as today."""
    assert boundary_verdict(_Env("totally_unknown_type"), "edit_result") == BOUNDARY_UNKNOWN
    assert boundary_verdict(_Env("new_file_destination"), None) == BOUNDARY_UNKNOWN


def test_world_facts_never_expire():
    """An executed covering-RED is valid whenever it exists — the same exemption
    _filter_candidates_by_phase makes. Expiring it would delete the strongest fact GT has."""
    assert boundary_verdict(_Env("covering_verdict"), "edit_result") == BOUNDARY_MATCH
    assert boundary_verdict(_Env("covering_verdict"), "search_result") == BOUNDARY_MATCH


def test_contracted_elsewhere_is_a_mismatch():
    assert boundary_verdict(_Env("new_file_destination"), "failed_search") == BOUNDARY_MATCH
    assert boundary_verdict(_Env("new_file_destination"), "edit_result") == BOUNDARY_MISMATCH


def test_steers_resolve_through_the_event_table():
    assert boundary_verdict(_Env("verify.horizon.pivot"), "test_result") == BOUNDARY_MATCH
    assert boundary_verdict(_Env("verify.horizon.pivot"), "edit_result") == BOUNDARY_MISMATCH


def test_the_three_verdicts_are_distinct():
    assert len({BOUNDARY_MATCH, BOUNDARY_MISMATCH, BOUNDARY_UNKNOWN}) == 3


# ---------------------------------------------------------------------------
# LEVER INTERACTION. Both levers touch selection, so testing them only in
# isolation is how a silent inversion survives — #29 already produced two.
#
# The dangerous shape: expiry DROPS the arbitration winner while a non-expired
# candidate was available and lost. That would silence a turn that had a valid
# fact. It cannot happen, because the two are COUPLED at the seam: _obs_boundary
# is set ONLY under GT_BOUNDARY_SPECIFICITY, and the expiry check requires it —
# so expiry can never run on an ordering that specificity did not produce, and
# specificity always sorts matched candidates ABOVE mismatched ones.
# ---------------------------------------------------------------------------

def test_specificity_prevents_expiry_from_silencing_a_valid_turn():
    """A matched candidate must never lose to a mismatched one and then be dropped."""
    envs = [_Env("new_file_destination"), _Env("caller_break")]      # 15 vs 48 statically
    winner = arbitrate(envs, frozenset(), "failed_search")
    assert winner.evidence_type == "new_file_destination"            # specificity overrides rank
    assert boundary_verdict(winner, "failed_search") == BOUNDARY_MATCH, "would be dropped"


def test_a_lone_expired_fact_is_dropped_and_that_is_correct():
    """Silence beats expired evidence. deliver_by is the LAST-useful boundary, so a fact past it
    no longer answers the observation — delivering it would be noise wearing a contract."""
    w = arbitrate([_Env("new_file_destination")], frozenset(), "test_result")
    assert boundary_verdict(w, "test_result") == BOUNDARY_MISMATCH


def test_world_fact_survives_both_levers_together():
    """The most damaging possible interaction: an executed covering-RED silently dropped."""
    w = arbitrate([_Env("covering_verdict"), _Env("caller_break")], frozenset(), "edit_result")
    assert w.evidence_type == "covering_verdict"
    assert boundary_verdict(w, "edit_result") == BOUNDARY_MATCH


def test_expiry_is_coupled_to_specificity_at_the_seam():
    """GT_BOUNDARY_EXPIRE alone must be inert: the seam only computes a boundary under
    GT_BOUNDARY_SPECIFICITY, and expiry requires one. Enabling expiry by itself must not start
    dropping facts against an ordering that never considered boundaries."""
    import os as _os
    src = open(_os.path.join(_os.path.dirname(__file__), "..", "gt_mini_patch.py"),
               encoding="utf-8").read()
    assert 'GT_BOUNDARY_EXPIRE", "0").strip() == "1" and _obs_boundary' in src, \
        "expiry no longer requires a resolved boundary — it could fire without specificity"
