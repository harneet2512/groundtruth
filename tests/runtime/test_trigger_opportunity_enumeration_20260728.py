"""The trigger denominator must be registry-derived, and must not lose features to it.

This pins the pure enumerator that answers "which fact triggers exist, and when does each
fire".  It is the denominator for every "dark feature" claim: without it, a producer that
was never asked and a producer that is broken are the same silence.

Three properties carry the whole design, and each one is a defect I made or nearly made:

1. **The authority is ``required_event``, not ``deliver_by``.**  The registry maps
   evidence types to a class BY DECISION, not by TIMING, so eight of the registered types
   deliver at a different boundary than their class declares.  Keying on ``deliver_by``
   attributes those to the WRONG observation — manufacturing phantom opportunities and
   phantom darkness at once.  My first design did exactly this.
2. **The class name is not in its own grain.**  ``evidence_grain_for(fc)`` returns only
   the FINER identifiers.  Enumerating the grain alone gives ``submit_refusal`` and
   ``syntax_result`` — two of the 17 DIRECT — zero triggers, because their grains hold
   only producer names that do not register.
3. **Unobservable boundaries are MARKED, never omitted.**  An omitted trigger is
   indistinguishable from one nobody considered, which is the exact failure this module
   exists to remove.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.runtime import trigger_opportunity as t  # noqa: E402
from groundtruth.runtime.fact_registry import (  # noqa: E402
    registration_for,
    required_event,
)


def _by_type() -> dict[str, t.TriggerSpec]:
    return {spec.evidence_type: spec for spec in t.all_triggers()}


def test_every_trigger_uses_required_event_as_its_boundary():
    """PROPERTY 1. The eight overrides are the reason this module exists."""
    for spec in t.all_triggers():
        assert spec.required_event == required_event(spec.evidence_type), (
            f"{spec.evidence_type} keyed on the wrong authority"
        )


@pytest.mark.parametrize(
    "evidence_type,boundary",
    [
        ("caller_break", "edit_result"),
        ("caller_contract_search", "search_result"),
        ("brief_localization", "task_start"),
        ("trace_frame", "failure_obs"),
        ("missing_role_postcreate", "edit_result"),
        ("obligation_unexercised", "test_result"),
        ("coherence_collapse", "edit_result"),
    ],
)
def test_the_known_overrides_are_carried_not_flattened(evidence_type, boundary):
    """Each of these DISAGREES with its class's deliver_by. A deliver_by-keyed design
    would attribute every one of them to the wrong observation."""
    spec = _by_type()[evidence_type]
    assert spec.required_event == boundary
    assert spec.deliver_by_overridden is True, (
        f"{evidence_type} must record that its boundary was overridden, so a future "
        f"reader that consults the wrong field is falsifiable from the row itself"
    )
    assert spec.declared_deliver_by != boundary


@pytest.mark.parametrize("evidence_type", ["submit_refusal", "syntax_result"])
def test_fact_classes_whose_grain_is_empty_still_get_a_trigger(evidence_type):
    """PROPERTY 2. Both are of the 17 DIRECT and both would be silently lost."""
    from groundtruth.runtime.fact_registry import evidence_grain_for

    spec = _by_type().get(evidence_type)
    assert spec is not None, f"{evidence_type} has NO trigger — it is one of the 17"
    assert spec.observable is True
    assert evidence_type not in evidence_grain_for(evidence_type), (
        "fixture precondition: the class must NOT be in its own grain, which is exactly "
        "why enumerating the grain alone loses it"
    )


def test_unobservable_boundaries_are_marked_with_a_reason_not_dropped():
    """PROPERTY 3. task_start and first_view_edit cannot be carried by an observation."""
    unobservable = [s for s in t.all_triggers() if not s.observable]
    assert unobservable, "the unobservable set must not be silently empty"
    for spec in unobservable:
        assert spec.required_event in {"task_start", "first_view_edit"}
        assert spec.unobservable_reason, f"{spec.evidence_type} marked without a reason"
    spec_types = {s.evidence_type for s in unobservable}
    assert "obligations" in spec_types, "the obligations CLASS delivers at task_start"


def test_obligations_is_still_measurable_through_its_finer_grain():
    """The consequence that saves a DIRECT feature.

    The ``obligations`` CLASS is contracted to ``task_start`` and is therefore
    unobservable — but ``obligation_unexercised`` overrides to ``test_result``, which IS
    derivable. A design keyed on the class alone would have marked the whole feature
    unmeasurable and lost the one grain that works.
    """
    spec = _by_type()["obligation_unexercised"]
    assert spec.fact_class == "obligations"
    assert spec.observable is True
    assert spec.required_event == "test_result"


def test_every_observable_trigger_lands_on_a_boundary_something_emits():
    """The import-time self-check, asserted explicitly.

    A registry row added at a boundary no semantic event carries would be silently
    unmeasurable — this module's own defect class, one level up.
    """
    for spec in t.observable_triggers():
        assert spec.required_event in t.DERIVABLE_BOUNDARIES


def test_triggers_for_event_is_the_emitters_whole_lookup():
    edit = t.triggers_for_event("edit_result")
    assert edit, "edit_result must carry triggers"
    assert all(s.required_event == "edit_result" for s in edit)
    assert {"syntax_result", "signature_mismatch"} <= {s.evidence_type for s in edit}
    assert not t.triggers_for_event("task_start"), (
        "an unobservable boundary must yield no triggers to emit against"
    )


def test_the_id_is_framed_and_stable():
    a = t.trigger_opportunity_id("a" * 64, "syntax_result")
    assert a == t.trigger_opportunity_id("a" * 64, "syntax_result")   # deterministic
    assert a != t.trigger_opportunity_id("a" * 64, "signature_mismatch")
    assert a != t.trigger_opportunity_id("b" * 64, "syntax_result")
    assert len(a) == 64
    for bad in (("", "x"), ("x", "")):
        with pytest.raises(ValueError):
            t.trigger_opportunity_id(*bad)


def test_enumeration_is_deterministic_and_registration_backed():
    first, second = t.all_triggers(), t.all_triggers()
    assert first == second
    assert [s.evidence_type for s in first] == sorted(s.evidence_type for s in first)
    for spec in first:
        assert registration_for(spec.evidence_type) is not None
