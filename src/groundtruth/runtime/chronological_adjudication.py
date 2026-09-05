"""Registry-owned chronological timing and fair-probe adjudication.

The adjudicator is pure and fail closed.  It can reject a delivery from exact
chronology alone, but it cannot manufacture causality from acknowledgment or a
subsequent action.  A positive causal verdict requires an exact, seal-bound
matched/shadow probe whose treatment and control outcomes differ.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .fact_registry import EVENTS, is_reactive, registration_for, required_event


ON_TIME = "ON_TIME"
LATE = "LATE"
# EARLY / LATE_CORRECTIVE / EXPIRED (2026-07-28) — the two bits the old vocabulary lost.
#
# EARLY: the bytes landed BEFORE the decision they answer was open. That used to return
# UNMEASURED, which is a lie of the same shape as reporting a replay error as "diffs=0" —
# rendering a precisely-known fact as an absence of knowledge.
#
# LATE_CORRECTIVE vs EXPIRED: `delivered > committed` fused two OPPOSITE outcomes. Evidence
# arriving after a commit the agent then REVISES is corrective and valuable; evidence
# arriving when nothing remains to change is worthless. One string, two meanings, no
# discriminating column — the same defect as the `acted` receipt field demoted earlier in
# this program.
#
# LATE is RETAINED, not replaced: historical artifacts carry the flat string and readers
# must still parse it.
EARLY = "EARLY"
LATE_CORRECTIVE = "LATE_CORRECTIVE"
EXPIRED = "EXPIRED"
STEP_BEHIND = "STEP_BEHIND"
WRONG_EVENT = "WRONG_EVENT"
UNMEASURED = "UNMEASURED"

CAUSAL = "CAUSAL"
SELF_LOCALIZED = "SELF_LOCALIZED"

TIMING_SCHEMA = "gt.chronological_timing.v1"
FAIR_PROBE_SCHEMA = "gt.fair_probe_result.v1"

_SHA16_WIDTH = 16
_SHA256_WIDTH = 64


def _lower_hex(value: object, width: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == width
        and all(char in "0123456789abcdef" for char in value)
    )


def _index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class Chronology:
    """Exact ordered indices for one candidate's intended decision."""

    decision_open_index: int | None
    delivery_index: int | None
    decision_commit_index: int | None
    native_acquisition_index: int | None
    acknowledgment_index: int | None
    action_index: int | None


@dataclass(frozen=True)
class MatchedProbe:
    """Exact assignment and outcome artifacts for one matched/shadow probe."""

    probe_id: str
    assignment_unit_id: str
    treatment_seal: str
    treatment_outcome: str
    control_outcome: str
    assignment_sha256: str
    treatment_sha256: str
    control_sha256: str
    outcome_sha256: str


@dataclass(frozen=True)
class Adjudication:
    evidence_type: str
    delivery_seal: str
    required_event: str
    actual_event: str
    timing_verdict: str
    fair_probe_verdict: str
    chronology: Chronology
    matched_probe: MatchedProbe | None

    def timing_bytes(self) -> bytes:
        return _canonical(
            {
                "schema": TIMING_SCHEMA,
                "evidence_type": self.evidence_type,
                "delivery_seal": self.delivery_seal,
                "required_event": self.required_event,
                "actual_event": self.actual_event,
                "verdict": self.timing_verdict,
                "chronology": asdict(self.chronology),
            }
        )

    def fair_probe_bytes(self) -> bytes:
        return _canonical(
            {
                "schema": FAIR_PROBE_SCHEMA,
                "evidence_type": self.evidence_type,
                "delivery_seal": self.delivery_seal,
                "verdict": self.fair_probe_verdict,
                "chronology": asdict(self.chronology),
                "matched_probe": (
                    asdict(self.matched_probe) if self.matched_probe is not None else None
                ),
            }
        )


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _timing(
    evidence_type: str,
    actual_event: str,
    chronology: Chronology,
) -> tuple[str, str]:
    wanted = required_event(evidence_type)
    registration = registration_for(evidence_type)
    if registration is None or wanted is None:
        return UNMEASURED, ""
    if actual_event not in EVENTS:
        # C-cluster split: a GENUINELY-UNKNOWN event (not in the registry vocabulary) is
        # unobservable — the join could not name what happened — so it stays UNMEASURED
        # (fail-closed, unchanged). This is the ONLY event path that remains UNMEASURED.
        return UNMEASURED, wanted
    # F (covering dual-label — DO NOT "fix"): the seam stamps ``covering_red`` deliveries with
    # actual_event='test_result' (the canonical class label) while the registry override routes
    # required_event('covering_red')='edit_result'; because covering_red ∈ _REACTIVE_EVIDENCE_TYPES,
    # is_reactive() is True here and the wrong-event check below is deliberately bypassed — the
    # RED is on-time-by-construction at the edit it targets. This is the intended contract; the
    # WRONG_EVENT split intentionally does NOT apply to reactive types.
    if not is_reactive(evidence_type) and actual_event != wanted:
        # C-cluster split: the event is KNOWN (in the vocabulary) but is the WRONG boundary for
        # a NON-reactive class — GT answered the wrong decision. That is a measured timing
        # FAILURE (correct_time=False), NOT an unmeasured gap; splitting it out of UNMEASURED
        # stops a real wrong-event fact from hiding behind "we couldn't measure it".
        return WRONG_EVENT, wanted
    opened = chronology.decision_open_index
    delivered = chronology.delivery_index
    committed = chronology.decision_commit_index
    if not all(_index(value) for value in (opened, delivered, committed)):
        return UNMEASURED, wanted
    assert opened is not None and delivered is not None and committed is not None
    if not opened <= committed:
        # The decision closed before it opened: the chronology is internally inconsistent,
        # so nothing about it is measurable. Fails closed, unchanged.
        return UNMEASURED, wanted
    if delivered < opened:
        # MEASURED, not unmeasurable: GT answered a decision that had not opened yet.
        #
        # NOTE this branch is currently unreachable from the extractor, and that is a
        # SEPARATE bug, not a reason to omit the verdict: `_decision_open_index` computes
        # `max(b for b in opens if b <= delivery_index)`, so `opened <= delivered` holds BY
        # CONSTRUCTION on real data. Measuring EARLY end-to-end requires the extractor to
        # ask whether ANY boundary of `earliest_event` exists at-or-before delivery. The
        # kernel must be able to SAY it before the extractor can find it.
        return EARLY, wanted
    acquired = chronology.native_acquisition_index
    if acquired is not None:
        if not _index(acquired):
            return UNMEASURED, wanted
        if acquired < delivered:
            return STEP_BEHIND, wanted
    # E (logical-clock defense-in-depth): a post-delivery ACTION cannot precede or coincide
    # with the delivery that caused it. ``action_index`` is derived only from messages STRICTLY
    # after ``delivery_index``, so ``action_index <= delivered`` is an impossible ordering; if it
    # ever appears the chronology is internally inconsistent and timing fails closed (UNMEASURED)
    # rather than trusting an inverted clock. Additive: None (no action) leaves the verdict as-is.
    acted = chronology.action_index
    if acted is not None:
        if not _index(acted):
            return UNMEASURED, wanted
        if acted <= delivered:
            return UNMEASURED, wanted
    if delivered > committed:
        # SPLIT THE TWO OPPOSITE MEANINGS OF "LATE".
        #
        # `acted` is the first post-delivery message that mutates or verifies while naming a
        # delivered entity (derived strictly AFTER delivery_index). Its presence means the
        # agent still had a decision left to change when the bytes arrived — the evidence was
        # late but could still act. Its absence means the decision was already final.
        #
        # HONEST LIMIT: this is a PROXY. The precise question is whether the same decision
        # COMMITS again after delivery, which needs the extractor to recompute
        # `_decision_commit_index` from `delivery_index`. `action_index` is the closest
        # quantity the Chronology already carries, and inventing a second clock here would be
        # worse than using the one that exists. The proxy is stated so a later reader can
        # tighten it rather than assume it is exact.
        return (LATE_CORRECTIVE if acted is not None else EXPIRED), wanted
    return ON_TIME, wanted


def _valid_probe(probe: MatchedProbe, delivery_seal: str) -> bool:
    return bool(
        probe.probe_id.strip()
        and probe.assignment_unit_id.strip()
        and probe.treatment_seal == delivery_seal
        and _lower_hex(probe.treatment_seal, _SHA16_WIDTH)
        and probe.treatment_outcome == "acted"
        and probe.control_outcome == "not_acted"
        and all(
            _lower_hex(value, _SHA256_WIDTH)
            for value in (
                probe.assignment_sha256,
                probe.treatment_sha256,
                probe.control_sha256,
                probe.outcome_sha256,
            )
        )
    )


def adjudicate(
    *,
    evidence_type: str,
    actual_event: str,
    delivery_seal: str,
    chronology: Chronology,
    matched_probe: MatchedProbe | None = None,
) -> Adjudication:
    """Return exact timing and causal states for one delivered candidate."""

    if not isinstance(chronology, Chronology):
        raise TypeError("chronology must be Chronology")
    if not _lower_hex(delivery_seal, _SHA16_WIDTH):
        raise ValueError("delivery seal must be exactly 16 lowercase hex")
    timing, wanted = _timing(evidence_type, actual_event, chronology)
    fair = UNMEASURED
    delivered = chronology.delivery_index
    acquired = chronology.native_acquisition_index
    if (
        _index(delivered)
        and _index(acquired)
        and acquired is not None
        and delivered is not None
        and acquired < delivered
    ):
        fair = SELF_LOCALIZED
    elif (
        timing == ON_TIME
        and matched_probe is not None
        and _valid_probe(matched_probe, delivery_seal)
        and _index(chronology.acknowledgment_index)
        and _index(chronology.action_index)
        and chronology.acknowledgment_index > chronology.delivery_index
        and chronology.action_index > chronology.delivery_index
    ):
        fair = CAUSAL
    return Adjudication(
        evidence_type=evidence_type,
        delivery_seal=delivery_seal,
        required_event=wanted,
        actual_event=actual_event,
        timing_verdict=timing,
        fair_probe_verdict=fair,
        chronology=chronology,
        matched_probe=matched_probe,
    )


__all__ = [
    "CAUSAL",
    "EARLY",
    "EXPIRED",
    "FAIR_PROBE_SCHEMA",
    "LATE",
    "LATE_CORRECTIVE",
    "ON_TIME",
    "SELF_LOCALIZED",
    "STEP_BEHIND",
    "TIMING_SCHEMA",
    "UNMEASURED",
    "WRONG_EVENT",
    "Adjudication",
    "Chronology",
    "MatchedProbe",
    "adjudicate",
]
