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
