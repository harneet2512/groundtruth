"""Which fact triggers were EVALUATED on an observation — the denominator for "dark".

WHY THIS EXISTS.  A GT feature that produced nothing is today indistinguishable from a
feature whose trigger never occurred.  Both look like silence, so "eleven features are
dark" cannot be graded: correct-quiet and broken read identically.  The fix is a
denominator — a host-side row per (observation, evidence_type) recording that a trigger
was evaluated and what the observation actually contained.  This module is the pure,
importable half: it answers *which triggers exist and when each one fires*, derived from
the registry, never hand-listed.

THE AUTHORITY IS ``required_event``, NOT ``deliver_by``.  This is the single most
important thing here and it is easy to get wrong — I did, on the first design.
``fact_registry`` maps evidence types to a canonical class BY DECISION, not by TIMING, so
a finer evidence type can deliver at a different boundary than its class:

    caller_break             deliver_by=file_view      required_event=edit_result
    caller_contract_search   deliver_by=file_view      required_event=search_result
    brief_localization       deliver_by=search_result  required_event=task_start
    trace_frame              deliver_by=search_result  required_event=failure_obs
    missing_role_postcreate  deliver_by=failed_search  required_event=edit_result
    obligation_unexercised   deliver_by=task_start     required_event=test_result
    coherence_collapse       deliver_by=failure_obs    required_event=edit_result

Seven of eighteen registered evidence types disagree, including two of the 17 DIRECT.  A
``deliver_by``-keyed enumerator would silently attribute those to the wrong observation —
manufacturing phantom opportunities (a trigger recorded as evaluated when the producer was
never asked) and phantom darkness (a producer that fired with no opportunity row).  Both
are worse than no row at all.

THE CLASS NAME IS NOT IN ITS OWN GRAIN.  ``evidence_grain_for(fc)`` returns the FINER
identifiers that collapse to ``fc``; it never includes ``fc`` itself.  Enumerating only
the grain gives ``submit_refusal`` and ``syntax_result`` — two of the 17 DIRECT — ZERO
triggers, because their grains contain only producer names with no registration.  So the
enumeration is ``{class} | grain``, filtered to what actually registers.

TWO BOUNDARIES ARE GENUINELY UNOBSERVABLE, and are marked rather than omitted:

* ``task_start`` — the step-0 brief reaches the model by raw string prepend, not as a tool
  observation at all, so no semantic event can carry it.
* ``first_view_edit`` — has NO producer anywhere in the runtime.  (A gateway comment
  claims ``patch_delta`` realizes it; that comment is false — ``patch_delta`` stamps
  ``edit_result``.)  Its only class, ``cochange_prior``, is an internal ranking prior that
  is never delivered as a fact.

Marking beats omitting: an omitted trigger is indistinguishable from one that was never
considered, which is the exact defect this module exists to remove.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from groundtruth.runtime.fact_registry import (
    EVENTS,
    all_fact_classes,
    evidence_grain_for,
    is_reactive,
    registration_for,
    required_event,
)

#: Boundaries no observation can carry. See the module docstring for why each.
_UNOBSERVABLE_BOUNDARIES: dict[str, str] = {
    "task_start": "step0_brief_is_not_an_observation",
    "first_view_edit": "no_producer_emits_this_boundary",
}

#: What ``gateway._observe_semantic_events`` can actually put on an observation. The
#: import-time self-check asserts every observable trigger lands inside this set, so a
#: registry row added at a boundary nothing emits fails LOUD instead of going quietly dark.
DERIVABLE_BOUNDARIES: frozenset[str] = frozenset({
    "edit_result", "test_result", "test_executed_no_tests", "file_view",
    "submit", "search_result", "failed_search", "failure_obs",
})

_SCHEMA = "gt.trigger_opportunity.v1"


@dataclass(frozen=True)
class TriggerSpec:
    """One evaluable trigger: an evidence type and the boundary it answers."""

    evidence_type: str
    fact_class: str
    required_event: str
    declared_deliver_by: str
    deliver_by_overridden: bool
    reactive: bool
    observable: bool
    unobservable_reason: str


def trigger_opportunity_id(observation_id: str, evidence_type: str) -> str:
    """Framed identity for one trigger evaluation inside one observation.

    FRAMED and fixed-width for the same reason the envelope ids are: concatenating
    variable-length fields without a frame lets two different inputs collide.
    """
    if not observation_id or not evidence_type:
        raise ValueError("observation_id and evidence_type must both be non-empty")
    return hashlib.sha256(
        _SCHEMA.encode("utf-8")
        + b"\x00"
        + hashlib.sha256(observation_id.encode("utf-8")).digest()
        + hashlib.sha256(evidence_type.encode("utf-8")).digest()
    ).hexdigest()


def _spec(evidence_type: str) -> TriggerSpec | None:
    registration = registration_for(evidence_type)
    if registration is None:
        return None
    wanted = required_event(evidence_type)
    if not wanted:
        return None
    declared = getattr(registration, "deliver_by", "") or ""
    fact_class = getattr(registration, "fact_class", "") or evidence_type
    reason = _UNOBSERVABLE_BOUNDARIES.get(wanted, "")
    return TriggerSpec(
        evidence_type=evidence_type,
        fact_class=fact_class,
        required_event=wanted,
        declared_deliver_by=declared,
        deliver_by_overridden=bool(declared and declared != wanted),
        reactive=bool(is_reactive(evidence_type)),
        observable=not reason,
        unobservable_reason=reason,
    )


def all_triggers() -> tuple[TriggerSpec, ...]:
    """Every registered trigger, observable or not, deterministically ordered."""
    seen: dict[str, TriggerSpec] = {}
    for fact_class in all_fact_classes():
        # ``{class} | grain`` -- the class is NEVER in its own grain, and omitting it
        # gives submit_refusal and syntax_result zero triggers.
        for evidence_type in {fact_class, *evidence_grain_for(fact_class)}:
            if evidence_type in seen:
                continue
            spec = _spec(evidence_type)
            if spec is not None:
                seen[evidence_type] = spec
    return tuple(seen[key] for key in sorted(seen))


def observable_triggers() -> tuple[TriggerSpec, ...]:
    """Only the triggers an observation can actually carry."""
    return tuple(spec for spec in all_triggers() if spec.observable)


def triggers_for_event(event: str) -> tuple[TriggerSpec, ...]:
    """Every trigger whose boundary is ``event`` — the emitter's whole lookup."""
    return tuple(
        spec for spec in observable_triggers() if spec.required_event == event
    )


def _self_check() -> None:
    """Fail LOUD at import if the registry drifts out of what can be observed.

    A registry row added at a boundary nothing emits would otherwise become silently
    unmeasurable -- the precise failure this module exists to prevent, reintroduced one
    level up. Better an import error than a feature that is dark for a reason nobody can
    see.
    """
    specs = all_triggers()
    if not specs:
        raise AssertionError("trigger_opportunity: registry produced no triggers")
    for spec in specs:
        if spec.required_event not in EVENTS:
            raise AssertionError(
                f"trigger_opportunity: {spec.evidence_type} declares required_event "
                f"{spec.required_event!r}, which is not in the registry vocabulary"
            )
        if spec.observable and spec.required_event not in DERIVABLE_BOUNDARIES:
            raise AssertionError(
                f"trigger_opportunity: {spec.evidence_type} is marked observable at "
                f"{spec.required_event!r}, but no semantic event carries that boundary. "
                f"Either add it to DERIVABLE_BOUNDARIES (and make the gateway emit it) "
                f"or record it in _UNOBSERVABLE_BOUNDARIES with a reason."
            )


_self_check()


__all__ = [
    "DERIVABLE_BOUNDARIES",
    "TriggerSpec",
    "all_triggers",
    "observable_triggers",
    "trigger_opportunity_id",
    "triggers_for_event",
]
