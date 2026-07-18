#!/usr/bin/env python3
"""receipt_predicates — the per-fact-class ACKNOWLEDGMENT evaluators (SS-LIVE Gate 4).

The consumption ledger's receipt LADDER (delivered -> referenced -> acted) is a generic DEPTH
record. It is NOT the registry-specific acknowledgment: a caller_contract fact is "acknowledged"
by an edit that PRESERVED the contract; a localization fact by OPENING the ranked file WITHOUT
first grep-searching for it; a recovery steer by PIVOTING. Each registered §1 fact class names
its own non-reacquisition receipt in ``fact_registry.FactRegistration.receipt_predicate``. This
module is the ONE deterministic evaluator per such predicate name.

``evaluate(receipt_predicate, ec, messages, ledger_rows, attestations) -> bool | None`` answers,
for ONE delivered chronology (``ec`` = an :class:`chronology_extract.ExtractedChronology`):
    * ``True``  — the agent acknowledged the fact in the class's registry-specific way,
    * ``False`` — the delivery was locatable (its decision opened + it was delivered) but the
                  agent did NOT acknowledge it (no commit / re-acquired it itself first / the
                  covering RED could not be tied to a targeted repair),
    * ``None``  — UNMEASURED (fail-closed): the delivery/decision could not even be located, so
                  the receipt is unobservable — never a guess.

It REUSES the ONE entity/action model (the chronology already computed the six exact indices via
``consumption_ledger._action_kind`` / ``_named_in`` / ``_block_entities``); it never forks a
second command classifier. PURE · DETERMINISTIC · LLM-FREE · stdlib-only.

The class rollup (``acknowledgment_by_fact_class``) is the Gate-4 value: a class PASSES (True)
only when EVERY delivered row's evaluator returned True; any False -> False; else (all None, or
True mixed with None) -> None (fail-closed UNMEASURED).
"""
from __future__ import annotations

from typing import Any, Iterable

from groundtruth.runtime.fact_registry import registration_for

# The producer-attestation PASS verdict token (the covering-RED completeness signal). Imported
# from the authority so a rename can never silently desync this reader.
try:  # pragma: no cover - import guard; producer_attestation is always present in-tree
    from groundtruth.runtime.producer_attestation import PASS as _ATTEST_PASS
except Exception:  # pragma: no cover
    _ATTEST_PASS = "PASS"

# The canonical registry receipt-predicate names, grouped by the acknowledgment SHAPE the
# chronology proves. Keyed by the exact ``receipt_predicate`` string on each §1 registration.
#
#   OPEN receipts — a localization / def-inspection fact is acknowledged by OPENING the ranked
#     or partitioned file the fact pointed at, WITHOUT the agent having self-acquired it first
#     (native_acquisition_index is None). If the agent grep-found the file before GT delivered
#     it, GT added no value -> the receipt is False even though the file was opened.
#   COMMIT receipts — an edit-shaping / plan / repair / pivot fact is acknowledged by COMMITTING
#     its decision (the chronology's ``decision_commit_index`` — the class-specific commit test
#     already computed in chronology_extract: a mutation naming the entity, a plan-prose step, or
#     a course-change pivot).
#   COVERING receipt — ``targeted_covering_failure`` additionally REQUIRES a complete covering
#     attestation bound to the delivery seal (the executed RED is real), then the same commit
#     test (the next repair targeted the covering/implicated file).
_OPEN_RECEIPTS: frozenset[str] = frozenset({
    "opened_ranked_file_without_search",
    "inspected_partitioned_def",
})
_COMMIT_RECEIPTS: frozenset[str] = frozenset({
    "plan_reflects_obligations",
    "preserved_caller_contract",
    "repaired_flagged_syntax",
    "updated_callers_for_delta",
    "halted_on_refusal",
    "created_at_precedent_path",
    "pivoted_after_steer",
    "opened_companion_file",  # cochange_prior (internal support; graded only if ever delivered)
})
_COVERING_RECEIPTS: frozenset[str] = frozenset({"targeted_covering_failure"})

ALL_RECEIPT_PREDICATES: frozenset[str] = (
    _OPEN_RECEIPTS | _COMMIT_RECEIPTS | _COVERING_RECEIPTS
)


def _self_check() -> None:
    """Fail LOUD at import if the registry declares a receipt_predicate this module does not
    evaluate (a new §1 class must never silently fall through to an un-graded Gate 4)."""
    from groundtruth.runtime.fact_registry import REGISTRY

    declared = {
        r.receipt_predicate for r in REGISTRY.values() if r.receipt_predicate
    }
    missing = declared - ALL_RECEIPT_PREDICATES
    if missing:
        raise ValueError(
            f"receipt_predicates: registry receipt predicate(s) {sorted(missing)} have no "
            f"evaluator (add them to _OPEN_/_COMMIT_/_COVERING_RECEIPTS)"
        )


def _measurable(ec: Any) -> bool:
    """True iff the delivery + its decision boundary were both located — the precondition for
    ANY receipt to be observable. Absent either -> the receipt is UNMEASURED (None)."""
    ch = getattr(ec, "chronology", None)
    if ch is None:
        return False
    return ch.delivery_index is not None and ch.decision_open_index is not None


def _committed(ec: Any) -> bool:
    return getattr(ec.chronology, "decision_commit_index", None) is not None


def _self_acquired(ec: Any) -> bool:
    return getattr(ec.chronology, "native_acquisition_index", None) is not None


def _covering_attestation_complete(
    ec: Any, attestations: Iterable[Any] | None
) -> bool:
    """True iff a producer attestation for the ``covering_red`` class is bound to THIS delivery
    seal AND its truth predicate(s) all PASS (the executed RED is a real, complete world fact).
    Fail-closed: absent/None attestations, or no matching complete bundle -> False."""
    if not attestations:
        return False
    seal = getattr(ec, "delivery_seal", "")
    for att in attestations:
        try:
            ev = getattr(att, "evidence_type", "")
            reg = registration_for(str(ev))
            if reg is None or reg.fact_class != "covering_red":
                continue
            if getattr(att, "delivery_seal", "") != seal:
                continue
            preds = getattr(att, "truth_predicates", ()) or ()
            if preds and all(
                getattr(p, "verdict", "") == _ATTEST_PASS for p in preds
            ):
                return True
        except Exception:  # noqa: BLE001 - a malformed bundle is never a proof
            continue
    return False


def evaluate(
    receipt_predicate: str,
    ec: Any,
    messages: list[dict] | None = None,
    ledger_rows: list[dict] | None = None,
    attestations: Iterable[Any] | None = None,
) -> bool | None:
    """Evaluate ONE registry receipt predicate for ONE delivered chronology. See module docstring.

    ``messages`` / ``ledger_rows`` are accepted for signature stability (future predicates that
    need raw trajectory context) — the current evaluators read the already-computed six-index
    chronology on ``ec``, the SAME entity/action model, so they never re-classify commands."""
    predicate = (receipt_predicate or "").strip()
    if not predicate or ec is None:
        return None
    if not _measurable(ec):
        return None  # UNMEASURED — the decision/delivery could not be located (fail-closed)

    if predicate in _OPEN_RECEIPTS:
        # opened the ranked/partitioned file (open-commit) AND did NOT self-acquire it first.
        return _committed(ec) and not _self_acquired(ec)

    if predicate in _COVERING_RECEIPTS:
        # the executed RED must be a complete attested world fact before a repair can "target"
        # it; without the attestation the covering-specific receipt is unobservable.
        if not _covering_attestation_complete(ec, attestations):
            return None
        return _committed(ec)

    if predicate in _COMMIT_RECEIPTS:
        return _committed(ec)

    # An unknown predicate name is not gradable here (fail-closed) — never a silent True.
    return None


def acknowledgment_for_row(
    ec: Any,
    messages: list[dict] | None = None,
    ledger_rows: list[dict] | None = None,
    attestations: Iterable[Any] | None = None,
) -> bool | None:
    """Resolve ``ec``'s registry receipt predicate and evaluate it. ``None`` when the row's
    evidence type resolves to no registered class (no predicate to grade)."""
    reg = registration_for(getattr(ec, "evidence_type", "") or "")
    if reg is None or not reg.receipt_predicate:
        return None
    return evaluate(reg.receipt_predicate, ec, messages, ledger_rows, attestations)


def roll_up(results: Iterable[bool | None]) -> bool | None:
    """Gate-4 class rollup: True only when every measured row is True (>=1 measured, no None,
    no False); False when any row is False; else None (all-None or True-mixed-with-None ->
    fail-closed UNMEASURED)."""
    materialized = list(results)
    if any(r is False for r in materialized):
        return False
    if materialized and all(r is True for r in materialized):
        return True
    return None


def acknowledgment_by_fact_class(
    chronologies: Iterable[Any],
    *,
    messages: list[dict] | None = None,
    ledger_rows: list[dict] | None = None,
    attestations: Iterable[Any] | None = None,
) -> dict[str, bool | None]:
    """The ``fact_class -> acknowledged`` map (Gate 4), rolled up over every delivered
    chronology (whole-row + compound-brief block). A class absent from the map was never
    delivered; ``None`` value = delivered-but-unmeasured (fail-closed)."""
    per_class: dict[str, list[bool | None]] = {}
    atts = list(attestations) if attestations is not None else None
    for ec in chronologies:
        fact_class = getattr(ec, "fact_class", None)
        if not isinstance(fact_class, str) or not fact_class:
            continue
        per_class.setdefault(fact_class, []).append(
            acknowledgment_for_row(ec, messages, ledger_rows, atts)
        )
    return {fc: roll_up(results) for fc, results in per_class.items()}


_self_check()


__all__ = [
    "ALL_RECEIPT_PREDICATES",
    "acknowledgment_by_fact_class",
    "acknowledgment_for_row",
    "evaluate",
    "roll_up",
]
