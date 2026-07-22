#!/usr/bin/env python3
"""SPEC-J4 — fair-probe diagnostics and live randomized-opportunity extraction.

The shadow instrument assigns each selected eligible candidate to DELIVER or HOLDOUT before its
outcome. A delivered candidate and a different withheld candidate in one trajectory are distinct
causal units, even when their fact class and pre-decision prefix match. This module therefore
keeps same-trajectory comparisons diagnostic, exports exact live opportunity records for a
task-clustered repeated study, and never promotes instrument presence into causal credit.

CLUSTER-4 STRENGTHENING (Gate-7 fair_probe). Five changes keep attribution fail-closed:

* B1 — ASSIGNMENT KEYED BY OPPORTUNITY. A pre-outcome assignment row carries one immutable
  observation/opportunity identity and one canonical candidate identity. Distinct candidates
  never become the same unit merely because their pre-decision bytes or fact class match.
* B2 — TREATMENT 'acted' = CLUSTER-3-PROVEN PRECOMMIT USE. ``acted`` iff (a) timing ON_TIME,
  (b) the registry receipt predicate evaluates True for the class, and (c) the action fell in
  the precommit window ``delivery_index < action_index <= decision_commit_index``. Post-hoc
  naming alone is no longer ``acted``.
* B3 — CONTROL 'not_acted' VIA THE SAME REGISTRY PREDICATE. ``not_acted`` iff the agent made
  >=1 decision in ``[withhold_index, next boundary)`` (the empty-window guard is KEPT — it
  prevents the ea0eb16c0 false-CAUSAL-from-censoring class) AND the class receipt predicate
  evaluates False on the control window. Receipt True anyway -> ``acted`` (self-acquired; probe
  invalid for CAUSAL). Withheld bytes never reach the grader (file anchor + hashes only).
* B4 — SAFE ONE-DECISION FORK for the safety-excluded classes. ``syntax_result`` /
  ``submit_refusal`` can never be held out, so a DISTINCT ``CAUSAL_FORK`` verdict is minted by a
  PURE RE-DERIVATION over a sealed checkpoint (the submit gate's ``gate_verdict`` / the lane's
  ``finalize_syntax_attestation``) with the GT candidate withheld from the model-visible surface
  — NO real submission, NO real mutation, NO episode branch. It carries an explicit bounded
  confidence (N=1 checkpoint) and a citation anchor, and is NEVER merged into the randomized
  CAUSAL rank.
* B5 — REJECT THE RESOLVED/UNRESOLVED SHORTCUT. ``CAUSAL_PAIRED`` (a bare resolution delta) no
  longer sets the ``fair_probe`` gate — it is ENRICHMENT only (reported, gate value ``None``).
  A deterministic ``CAUSAL_FORK`` is mechanism evidence and cannot set the behavioral gate.

REUSE, NOT REINVENT (the reader/writer-mismatch discipline). Every join reuses the landed J3
machinery, the Cluster-3 registry receipt evaluators, and the consumption-ledger entity model
the grader already trusts:

* the TREATMENT arm is read straight from J3's ``extract_chronologies`` output — this module
  never re-derives the six indices.
* the CONTROL arm reads the withheld instance from its shadow-holdout row. The withheld bytes
  NEVER reach the grader (leak-safe: only the row's HASH + ``file_path`` survive), so the
  control's entity anchor is the row's ``file_path`` — the SAME fallback J3 uses when a payload
  is unavailable. The registry receipt is re-evaluated over a control chronology built with the
  SAME ``chronology_extract`` derivations (``_decision_open_index`` / ``_decision_commit_index``
  / ``_native_acquisition_index``), so there is never a second, divergent join.
* the message-index space + observation boundaries + tool-ordinal map are reused verbatim from
  ``chronology_extract``.

PAIRED-BASELINE PATH (safety-excluded classes only). ``syntax_result`` / ``submit_refusal``
can NEVER have a holdout row by design. Their OPTIONAL paired-baseline verdict (``CAUSAL_PAIRED``)
rides a caller-supplied resolution delta and, per B5, is ENRICHMENT ONLY (it no longer sets the
gate). The B4 safety fork records deterministic mechanism influence only.

PURE · DETERMINISTIC · fail-closed. The only side effect is an OPTIONAL sealed sidecar written
to the caller-designated output dir; every write is wrapped so a fault never breaks the grader.

NOTE — the sidecar write DELIBERATELY breaks the otherwise read-only-grader convention: it is
spec-mandated (the "seal-bound causal result" the doctrine demands), fail-closed (any fault ->
no path, never an exception into the grader), and inert to grader inputs — its
``gt_fair_probe_results_<task>.json`` name is targeted by no existing artifact glob, so it never
feeds back into any collector's inputs.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from groundtruth.runtime.chronological_adjudication import (
    CAUSAL,
    ON_TIME,
    SELF_LOCALIZED,
    UNMEASURED,
    Chronology,
    MatchedProbe,
    adjudicate,
)
from groundtruth.runtime.fact_registry import (
    registration_for,
    required_event,
)
from groundtruth.runtime.shadow_holdout import (
    PARTICIPATING_CLASSES,
    SAFETY_EXCLUDED_CLASSES,
    canonical_class,
)

# REUSE the landed J3 join machinery (do NOT reinvent a second divergent join).
from chronology_extract import (
    ExtractedChronology,
    _boundary_indices,
    _decision_commit_index,
    _decision_open_index,
    _delivery_entity_patterns,
    _messages,
    _native_acquisition_index,
    _tool_ordinal_to_index,
    _valid_seal,
    extract_chronologies,
)

# REUSE the grader-side entity model (the SAME one J3 uses for native acquisition / action).
# ``_action_kind`` is the SAME command classifier the consumption ledger uses for receipt-3
# (mutation / verification / else), so the continuous consequence measurement never invents a
# second divergent notion of what an edit or a test run is.
from consumption_ledger import (
    _action_kind,
    _emitted_commands,
    _entity_patterns,
    _named_in,
)

# REUSE the Cluster-3 registry receipt evaluators (per-class acknowledgment; do NOT re-classify).
import receipt_predicates

# The adjudicator's own schema string is the result schema (this is the adjudicated result).
FAIR_PROBE_RESULT_SCHEMA = "gt.fair_probe_result.v1"

# The paired-baseline verdict — DISTINCT from the randomized CAUSAL, never conflated. The
# adjudicator has no concept of it (it has no randomized control), so it is set here directly.
# B5: it is ENRICHMENT ONLY — it does NOT set the fair_probe gate.
CAUSAL_PAIRED = "CAUSAL_PAIRED"

# B4: the safety-fork verdict — minted by a PURE re-derivation over a sealed checkpoint for a
# safety-excluded class (which can never be held out).  It proves a deterministic mechanism
# changed its own decision.  It is NOT agent-behaviour causality and therefore cannot set the
# behavioural fair-probe gate.
CAUSAL_FORK = "CAUSAL_FORK"

# verdict precedence for the per-class rollup. A valid adjudication (CAUSAL / CAUSAL_FORK) wins;
# CAUSAL_PAIRED (enrichment) sits below CAUSAL so a real randomized CAUSAL is never masked by an
# enrichment verdict; SELF_LOCALIZED beats UNMEASURED; UNMEASURED loses. CAUSAL_FORK is the top
# rank so a safety class's fork verdict wins over a co-present CAUSAL_PAIRED enrichment.
_VERDICT_RANK = {
    UNMEASURED: 1,
    SELF_LOCALIZED: 2,
    CAUSAL_PAIRED: 3,
    CAUSAL: 4,
    CAUSAL_FORK: 5,
}


def _canonical(value: object) -> bytes:
    """The adjudicator's canonical-JSON encoding (byte-identical, deterministic)."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


# --------------------------------------------------------------------------- #
# B1 — pre-decision-state identity (the pairing key)
# --------------------------------------------------------------------------- #
def _predecision_state_id(
    canon: str | None,
    anchor_index: int | None,
    boundaries: dict[str, list[int]],
    messages: list[dict],
) -> str | None:
    """The PRE-DECISION STATE identity for one arm of a probe:
    ``sha256(decision_key || open_event || boundary_prefix_hash)``.

    * ``decision_key`` = the class's ``registration.target_decision`` (WHICH decision),
    * ``open_event``  = the class's ``required_event`` (WHICH observation boundary opens it),
    * ``boundary_prefix_hash`` = sha256 of the trajectory prefix up to AND INCLUDING the
      ``decision_open_index`` (the observation stream at decision open, before delivery /
      withholding). The decision-open index is the GREATEST boundary of ``open_event`` at-or-
      before ``anchor_index`` — the delivery index (treatment) or the withhold index (control).

    Both arms are resolved through the CANONICAL class (the only identity a delivered fact and a
    withheld holdout row share), so a finer-typed treatment still pairs with a canonical-class
    control. Two arms pair iff their pre-decision states are IDENTICAL — same class, same opening
    boundary, byte-identical observation prefix. ``None`` (fail-closed -> never pairs) when the
    class is unregistered, the boundary never opened before the anchor, or the anchor is absent.
    """
    if not isinstance(anchor_index, int) or isinstance(anchor_index, bool) or anchor_index < 0:
        return None
    reg = registration_for(canon or "")
    if reg is None:
        return None
    open_event = required_event(canon or "")
    if not open_event:
        return None
    decision_open = _decision_open_index(open_event, anchor_index, boundaries)
    if decision_open is None:
        return None
    prefix = messages[: decision_open + 1]
    prefix_hash = hashlib.sha256(_canonical(prefix)).hexdigest()
    return hashlib.sha256(
        _canonical([reg.target_decision, open_event, prefix_hash])
    ).hexdigest()


# --------------------------------------------------------------------------- #
# result records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ControlResult:
    """The withheld arm's observed decision window + its outcome (leak-safe: file anchor only)."""

    holdout_row_index: int
    fact_class: str
    control_seal: str
    withhold_index: int | None
    window_end: int | None
    entity_named: bool | None
    receipt: bool | None
    predecision_state_id: str | None
    opportunity_id: str | None
    outcome: str  # "not_acted" | "acted" | "unresolved"


@dataclass(frozen=True)
class ProbeResult:
    """One adjudicated fair-probe result (randomized / paired-baseline / safety-fork)."""

    probe_kind: str  # "randomized" | "paired_baseline" | "safety_fork"
    fact_class: str
    delivered_row_index: int | None
    holdout_row_index: int | None
    treatment_seal: str
    control_seal: str
    treatment_outcome: str
    control_outcome: str
    causal_verdict: str
    matched_probe: MatchedProbe | None
    artifacts: dict[str, Any]
    bounded_confidence: str = ""  # B4: explicit N=1 checkpoint confidence for the safety fork

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_kind": self.probe_kind,
            "fact_class": self.fact_class,
            "delivered_row_index": self.delivered_row_index,
            "holdout_row_index": self.holdout_row_index,
            "treatment_seal": self.treatment_seal,
            "control_seal": self.control_seal,
            "treatment_outcome": self.treatment_outcome,
            "control_outcome": self.control_outcome,
            "causal_verdict": self.causal_verdict,
            "bounded_confidence": self.bounded_confidence,
            "matched_probe": (
                asdict(self.matched_probe) if self.matched_probe is not None else None
            ),
            "artifacts": self.artifacts,
        }


# --------------------------------------------------------------------------- #
# arm computations
# --------------------------------------------------------------------------- #
def _treatment_acted(
    ec: Any,
    messages: list[dict] | None = None,
    ledger_rows: list[dict] | None = None,
    attestations: Any = None,
) -> bool:
    """B2: the delivered instance was CONSUMED at the decision point, proven three ways:
    (a) the timing verdict is ON_TIME (WRONG_EVENT / LATE / STEP_BEHIND are excluded upstream by
        the adjudicator), (b) the class's REGISTRY receipt predicate evaluates True (the
        Cluster-3 acknowledgment — an edit that preserved a contract, a plan reflecting the
        obligation, a pivot after a steer — NOT a bare mention), and (c) the action fell in the
        PRECOMMIT-USE window ``delivery_index < action_index <= decision_commit_index`` (the
        agent acted on the fact BEFORE committing the decision it was meant to shape).
    Post-hoc naming alone is no longer ``acted``."""
    if getattr(ec, "timing_verdict", None) != ON_TIME:
        return False
    ack = receipt_predicates.acknowledgment_for_row(ec, messages, ledger_rows, attestations)
    if ack is not True:
        return False
    ch = ec.chronology
    d = ch.delivery_index
    a = ch.action_index
    c = ch.decision_commit_index
    return bool(
        isinstance(d, int) and not isinstance(d, bool)
        and isinstance(a, int) and not isinstance(a, bool)
        and isinstance(c, int) and not isinstance(c, bool)
        and d < a <= c
    )


def _self_localized(chronology) -> bool:
    """The agent self-acquired the fact BEFORE GT delivered it (the adjudicator's SELF_LOCALIZED
    condition): a valid native-acquisition index strictly before delivery."""
    delivered = chronology.delivery_index
    native = chronology.native_acquisition_index
    return bool(
        isinstance(delivered, int)
        and isinstance(native, int)
        and native < delivered
    )


def _holdout_class(row: dict) -> str | None:
    """Resolve a shadow-holdout row to its canonical fact class (fact_class side-car first,
    then the seam ``layer``/``kind`` label). Only a PARTICIPATING class can be a holdout."""
    for key in ("fact_class", "layer", "kind", "evidence_type"):
        raw = row.get(key)
        if isinstance(raw, str) and raw.strip():
            canon = canonical_class(raw)
            if canon is not None and canon in PARTICIPATING_CLASSES:
                return canon
    return None


def _opportunity_id(row: dict) -> str | None:
    """The immutable randomized unit, never inferred from class/state similarity."""
    binding = row.get("observation_binding")
    value = binding.get("opportunity_id") if isinstance(binding, dict) else None
    if not isinstance(value, str) or len(value) != 64:
        return None
    return value if all(char in "0123456789abcdef" for char in value) else None


def _control_receipt(
    canon: str,
    withhold_index: int,
    window_end: int | None,
    control_seal: str,
    file_path: str,
    messages: list[dict],
    boundaries: dict[str, list[int]],
    ledger_rows: list[dict] | None,
    attestations: Any,
) -> bool | None:
    """B3: evaluate the class's REGISTRY receipt predicate over the CONTROL window (leak-safe).

    Build a control chronology for the withheld instance with the SAME ``chronology_extract``
    derivations the treatment used — decision opens at the class's ``required_event`` boundary
    at-or-before the withholding point; the commit / native-acquisition are searched within the
    control window (``messages[:window_end]``) using the file-path ENTITY ANCHOR (the withheld
    payload never reaches the grader). Then run the Cluster-3 receipt evaluator. ``True`` = the
    agent achieved the class-specific acknowledgment WITHOUT GT (self-acquired -> probe invalid);
    ``False`` = it did not; ``None`` = the receipt is unobservable (fail-closed)."""
    open_event = required_event(canon or "")
    if not open_event:
        return None
    decision_open = _decision_open_index(open_event, withhold_index, boundaries)
    if decision_open is None:
        return None
    msgs = messages[:window_end] if isinstance(window_end, int) else messages
    pats = _delivery_entity_patterns("", file_path)  # payload withheld -> file anchor
    decision_commit = _decision_commit_index(msgs, decision_open, canon, pats)
    native = _native_acquisition_index(msgs, withhold_index, "", file_path)
    chron = Chronology(
        decision_open_index=decision_open,
        delivery_index=withhold_index,
        decision_commit_index=decision_commit,
        native_acquisition_index=native,
        acknowledgment_index=None,
        action_index=None,
    )
    control_ec = ExtractedChronology(
        ledger_row_index=-1,
        evidence_type=canon,
        fact_class=canon,
        delivery_seal=control_seal,
        actual_event=open_event,
        chronology=chron,
        timing_verdict=UNMEASURED,
        unmeasured_reason=None,
    )
    try:
        return receipt_predicates.acknowledgment_for_row(
            control_ec, msgs, ledger_rows, attestations
        )
    except Exception:  # noqa: BLE001 — a malformed control chronology is never a proof
        return None


def _control_outcome(
    row: dict,
    row_index: int,
    fact_class: str,
    messages: list[dict],
    tool_ordinal_index: dict[int, int],
    boundaries: dict[str, list[int]],
    ledger_rows: list[dict] | None,
    attestations: Any,
) -> ControlResult:
    """Grade the withheld arm (B3). ``not_acted`` iff the withholding point is located AND a
    valid withheld-render hash is present AND the withheld entity resolves AND the agent
    INSPECTED at least one decision in the window (an assistant command-bearing message between
    the withholding point and the NEXT observation boundary) AND the class REGISTRY receipt
    predicate evaluates FALSE on that control window. ``acted`` iff the receipt evaluates True
    (the agent self-acquired the fact anyway -> GT not needed). ``unresolved`` (fail-closed,
    ``_valid_probe`` rejects it -> never CAUSAL) when the withholding point / hash / entity
    cannot be located, the window held ZERO agent decisions, OR the receipt is unmeasurable.

    The empty-window guard is load-bearing: an EMPTY window (withholding at end-of-trajectory,
    or a boundary adjacent to the withholding point) means the agent made NO decision GT could
    have changed — an absence of activity is NOT evidence the agent 'did not self-acquire'.
    Reading it as ``not_acted`` would mint a false CAUSAL from censoring (the ea0eb16c0
    fabrication class on the terminal gate); it is ``unresolved`` instead."""
    seal = row.get("content_sha256_16")
    control_seal = str(seal) if _valid_seal(seal) else ""

    withhold_index: int | None = None
    it = row.get("iteration")
    if isinstance(it, int) and not isinstance(it, bool):
        withhold_index = tool_ordinal_index.get(it)

    # Leak-safe: the withheld bytes never reach the grader — the entity anchor is the row's
    # file_path (the SAME fallback J3 uses when a delivered payload is unavailable).
    file_path = str(row.get("file_path") or "")
    files: set[str] = set()
    if file_path:
        files = {file_path, os.path.basename(file_path)}
    pats = _entity_patterns(files, set())

    predecision = _predecision_state_id(fact_class, withhold_index, boundaries, messages)

    # The shadow instrument ALWAYS seals the withheld render (shadow_holdout invariant 5); a
    # hash-less row is malformed -> unresolved (a control arm without a valid withheld-hash is
    # never accepted into a randomized probe).
    if withhold_index is None or not pats or not _valid_seal(control_seal):
        return ControlResult(
            holdout_row_index=row_index, fact_class=fact_class, control_seal=control_seal,
            withhold_index=withhold_index, window_end=None, entity_named=None,
            receipt=None, predecision_state_id=predecision,
            opportunity_id=_opportunity_id(row), outcome="unresolved",
        )

    # the decision window closes at the NEXT observation boundary after the withholding point.
    flat_boundaries = sorted({i for lst in boundaries.values() for i in lst})
    window_end = next((b for b in flat_boundaries if b > withhold_index), len(messages))
    inspected_decision = False
    entity_named = False
    for j in range(withhold_index + 1, window_end):
        if messages[j].get("role") != "assistant":
            continue
        commands = _emitted_commands(messages[j])
        if commands:
            inspected_decision = True
        for cmd in commands:
            if _named_in(cmd, pats):
                entity_named = True
                break

    if not inspected_decision:
        # empty / activity-free window: no agent decision to be a counterfactual over.
        return ControlResult(
            holdout_row_index=row_index, fact_class=fact_class, control_seal=control_seal,
            withhold_index=withhold_index, window_end=window_end, entity_named=entity_named,
            receipt=None, predecision_state_id=predecision,
            opportunity_id=_opportunity_id(row), outcome="unresolved",
        )

    receipt = _control_receipt(
        fact_class, withhold_index, window_end, control_seal, file_path,
        messages, boundaries, ledger_rows, attestations,
    )
    # A PRIOR self-acquisition of the withheld entity (a passive grep/read naming it strictly
    # BEFORE the withholding point) means the agent already had the fact — withholding cost
    # nothing, so the control is contaminated and can never be a clean counterfactual. Grade it
    # "acted" (probe invalid) regardless of the in-window receipt (correct-or-quiet: never a false
    # CAUSAL from a self-supplied control). This is the "control self-acquired invalidates" guard.
    self_acquired = _native_acquisition_index(messages, withhold_index, "", file_path) is not None
    if receipt is True or self_acquired:
        outcome = "acted"       # self-acquired (in-window receipt or prior acquisition) -> invalid
    elif receipt is False:
        outcome = "not_acted"    # inspected >=1 decision, did NOT self-acquire -> valid control
    else:
        outcome = "unresolved"   # receipt unmeasurable -> fail-closed

    return ControlResult(
        holdout_row_index=row_index, fact_class=fact_class, control_seal=control_seal,
        withhold_index=withhold_index, window_end=window_end, entity_named=entity_named,
        receipt=receipt, predecision_state_id=predecision,
        opportunity_id=_opportunity_id(row), outcome=outcome,
    )


def _build_matched_probe(
    ec: ExtractedChronology,
    fact_class: str,
    treatment_outcome: str,
    control: ControlResult,
    predecision_state_id: str | None,
    paired: bool,
    *,
    treatment_opportunity_id: str | None = None,
) -> tuple[MatchedProbe | None, dict[str, Any]]:
    """Seal the four canonical artifacts (assignment, treatment, control, outcome) and bind them
    into a ``MatchedProbe`` — but ONLY when the two arms share a pre-decision state (B1). The
    ``assignment_unit_id`` IS the shared ``predecision_state_id`` (never a bare fact_class). When
    the arms do NOT pair (different pre-decision state, or either state unresolved) NO
    ``MatchedProbe`` is bound (``None``) so the adjudicator can never return CAUSAL — the audit
    artifacts still record the mismatch."""
    assignment_unit_id = predecision_state_id or ""
    assignment_artifact = {
        "schema": "gt.fair_probe.assignment.v1",
        "fact_class": fact_class,
        "assignment_unit_id": assignment_unit_id,
        "paired": bool(paired),
        "treatment_predecision_state_id": predecision_state_id,
        "control_predecision_state_id": control.predecision_state_id,
        "treatment_opportunity_id": treatment_opportunity_id,
        "control_opportunity_id": control.opportunity_id,
        "treatment_seal": ec.delivery_seal,
        "control_seal": control.control_seal,
        "treatment_row_index": ec.ledger_row_index,
        "control_row_index": control.holdout_row_index,
    }
    treatment_artifact = {
        "schema": "gt.fair_probe.treatment.v1",
        "seal": ec.delivery_seal,
        "delivery_index": ec.chronology.delivery_index,
        "acknowledgment_index": ec.chronology.acknowledgment_index,
        "action_index": ec.chronology.action_index,
        "decision_commit_index": ec.chronology.decision_commit_index,
        "timing_verdict": ec.timing_verdict,
        "outcome": treatment_outcome,
    }
    control_artifact = {
        "schema": "gt.fair_probe.control.v1",
        "seal": control.control_seal,
        "withhold_index": control.withhold_index,
        "window_end": control.window_end,
        "entity_named": control.entity_named,
        "receipt": control.receipt,
        "outcome": control.outcome,
    }
    outcome_artifact = {
        "schema": "gt.fair_probe.outcome.v1",
        "treatment_outcome": treatment_outcome,
        "control_outcome": control.outcome,
        "paired": bool(paired),
        "differ": treatment_outcome != control.outcome,
    }
    artifacts = {
        "assignment": assignment_artifact,
        "treatment": treatment_artifact,
        "control": control_artifact,
        "outcome": outcome_artifact,
    }
    if not paired or not assignment_unit_id:
        return None, artifacts
    probe = MatchedProbe(
        probe_id=f"{fact_class}:{ec.delivery_seal}:{control.control_seal}",
        assignment_unit_id=assignment_unit_id,
        treatment_seal=ec.delivery_seal,
        treatment_outcome=treatment_outcome,
        control_outcome=control.outcome,
        assignment_sha256=_sha256(assignment_artifact),
        treatment_sha256=_sha256(treatment_artifact),
        control_sha256=_sha256(control_artifact),
        outcome_sha256=_sha256(outcome_artifact),
    )
    return probe, artifacts


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def compute_matched_probes(
    trajectory: Any,
    ledger_rows: list[dict],
    chronologies: dict[int, ExtractedChronology],
    *,
    attestations: Any = None,
) -> list[ProbeResult]:
    """Build diagnostic arm comparisons without fabricating a same-unit counterfactual.

    A delivered and a withheld candidate in one trajectory are mutually exclusive randomized
    opportunities.  Even at a byte-identical pre-decision prefix they are different candidates,
    and the delivered candidate can affect the withheld candidate's consequence window.  They
    therefore cannot be bound into one ``MatchedProbe``.  The rows remain useful diagnostics;
    causal estimation belongs to a preregistered cross-opportunity cohort or repeated exact-state
    intervention. Deterministic order: by (delivered_row_index, holdout_row_index)."""
    messages = _messages(trajectory)
    tool_ordinal_index = _tool_ordinal_to_index(messages)
    boundaries = _boundary_indices(messages)

    holdouts_by_class: dict[str, list[ControlResult]] = {}
    for row_index, row in enumerate(ledger_rows):
        if not isinstance(row, dict) or row.get("outcome") != "shadow_holdout":
            continue
        canon = _holdout_class(row)
        if canon is None:
            continue
        control = _control_outcome(
            row, row_index, canon, messages, tool_ordinal_index,
            boundaries, ledger_rows, attestations,
        )
        holdouts_by_class.setdefault(canon, []).append(control)

    results: list[ProbeResult] = []
    for row_index in sorted(chronologies):
        ec = chronologies[row_index]
        canon = canonical_class(ec.fact_class or ec.evidence_type or "")
        if canon is None or canon not in PARTICIPATING_CLASSES:
            continue
        if not _valid_seal(ec.delivery_seal):
            continue
        controls = holdouts_by_class.get(canon)
        if not controls:
            continue
        treatment_outcome = (
            "acted"
            if _treatment_acted(ec, messages, ledger_rows, attestations)
            else "not_acted"
        )
        treatment_state = _predecision_state_id(
            canon, ec.chronology.delivery_index, boundaries, messages
        )
        treatment_opportunity = _opportunity_id(ledger_rows[ec.ledger_row_index])
        for control in sorted(controls, key=lambda c: c.holdout_row_index):
            # A single opportunity cannot be both DELIVER and HOLDOUT in one realized
            # trajectory.  State/class similarity is stratification, not a matched unit.
            #
            # CLASS-5 CONTRACT (verified 2026-07-18 — NOT a bug). ``paired=False`` is load-bearing,
            # not a stub: a delivered candidate and a distinct withheld candidate are mutually
            # exclusive randomized units (B1), so ``adjudicate`` receives an UNPAIRED probe and can
            # never mint a behavioral ``CAUSAL`` here — the ONLY verdict ``_verdict_to_bool`` maps to
            # fair_probe=True. A behavioral fair_probe=True therefore AWAITS the preregistered
            # cross-opportunity cohort / exact-state replay arm (the ``paired=True`` counterfactual),
            # which is deliberately not yet wired. This does NOT leave the safety classes blocked: the
            # fork-capable classes (``syntax_result`` / ``submit_refusal``) mint their OWN
            # ``CAUSAL_FORK`` via ``_safety_fork_probes`` — a pure sealed-checkpoint re-derivation that
            # needs NO holdout row, so it PRODUCES mechanism evidence even at shadow_rate=0 (proven:
            # test_submit_refusal_fork_is_mechanism_evidence_not_behavior_gate). By B5, that
            # ``CAUSAL_FORK`` is INTENTIONALLY gate-None (mechanism evidence, never the behavioral
            # gate — ``_verdict_to_bool``), so we must NOT wire fork verdicts to True. With no holdout
            # rows (shadow_rate=0) the randomized path is inert and fair_probe stays UNMEASURED — the
            # correct fail-closed posture.
            paired = False
            probe, artifacts = _build_matched_probe(
                ec, canon, treatment_outcome, control, treatment_state, paired,
                treatment_opportunity_id=treatment_opportunity,
            )
            artifacts["assignment"]["same_predecision_state"] = bool(
                treatment_state is not None
                and control.predecision_state_id is not None
                and treatment_state == control.predecision_state_id
            )
            artifacts["assignment"]["pairing_rejection"] = (
                "distinct_mutually_exclusive_opportunities_require_cohort_or_replay"
            )
            verdict = adjudicate(
                evidence_type=ec.evidence_type,
                actual_event=ec.actual_event,
                delivery_seal=ec.delivery_seal,
                chronology=ec.chronology,
                matched_probe=probe,  # None when unpaired -> never CAUSAL
            ).fair_probe_verdict
            results.append(
                ProbeResult(
                    probe_kind="randomized",
                    fact_class=canon,
                    delivered_row_index=ec.ledger_row_index,
                    holdout_row_index=control.holdout_row_index,
                    treatment_seal=ec.delivery_seal,
                    control_seal=control.control_seal,
                    treatment_outcome=treatment_outcome,
                    control_outcome=control.outcome,
                    causal_verdict=verdict,
                    matched_probe=probe,
                    artifacts=artifacts,
                )
            )
    return results


def _paired_baseline_probes(
    chronologies: dict[int, ExtractedChronology],
    gt_resolved: bool | None,
    baseline_resolved: bool | None,
    *,
    messages: list[dict] | None = None,
    ledger_rows: list[dict] | None = None,
    attestations: Any = None,
) -> list[ProbeResult]:
    """The paired-baseline path for SAFETY-excluded classes (no holdout possible). Fires ONLY
    when the caller supplies ``gt_resolved is True`` and ``baseline_resolved is False`` (a real
    GT-on lift over baseline) AND the class delivered ON_TIME with B2 precommit-use ``acted``.
    Absent / weaker input -> no probe (UNMEASURED). A same-row SELF_LOCALIZED chronology (the
    agent self-acquired the fact before GT delivered it) is NEVER upgraded — GT was not the cause.
    B5: the emitted verdict ``CAUSAL_PAIRED`` is ENRICHMENT ONLY; it does NOT set the gate."""
    if gt_resolved is not True or baseline_resolved is not False:
        return []
    results: list[ProbeResult] = []
    for row_index in sorted(chronologies):
        ec = chronologies[row_index]
        canon = canonical_class(ec.fact_class or ec.evidence_type or "")
        if canon is None or canon not in SAFETY_EXCLUDED_CLASSES:
            continue
        if ec.timing_verdict != ON_TIME or not _treatment_acted(
            ec, messages, ledger_rows, attestations
        ):
            continue
        # honesty guard: prior self-acquisition means GT was not the cause; do not let the paired
        # verdict outrank the SELF_LOCALIZED signal for the same class.
        if _self_localized(ec.chronology):
            continue
        assignment_artifact = {
            "schema": "gt.fair_probe.paired_assignment.v1",
            "fact_class": canon,
            "treatment_seal": ec.delivery_seal,
            "treatment_row_index": ec.ledger_row_index,
        }
        treatment_artifact = {
            "schema": "gt.fair_probe.treatment.v1",
            "seal": ec.delivery_seal,
            "delivery_index": ec.chronology.delivery_index,
            "acknowledgment_index": ec.chronology.acknowledgment_index,
            "action_index": ec.chronology.action_index,
            "outcome": "acted",
        }
        outcome_artifact = {
            "schema": "gt.fair_probe.paired_outcome.v1",
            "gt_resolved": True,
            "baseline_resolved": False,
            "verdict": CAUSAL_PAIRED,
        }
        artifacts = {
            "assignment": assignment_artifact,
            "treatment": treatment_artifact,
            "baseline": outcome_artifact,
        }
        results.append(
            ProbeResult(
                probe_kind="paired_baseline",
                fact_class=canon,
                delivered_row_index=ec.ledger_row_index,
                holdout_row_index=None,
                treatment_seal=ec.delivery_seal,
                control_seal="",
                treatment_outcome="acted",
                control_outcome="baseline_unresolved",
                causal_verdict=CAUSAL_PAIRED,
                matched_probe=None,
                artifacts=artifacts,
            )
        )
    return results


# --------------------------------------------------------------------------- #
# B4 — the safe one-decision fork (CAUSAL_FORK) for the safety-excluded classes
# --------------------------------------------------------------------------- #
def _fork_checkpoint_valid(cp: Any) -> bool:
    """The sealed checkpoint identity must be ``sha256(observation_id || prefix_hash ||
    repo_state_hash)`` — a re-derivation the fork can VERIFY. A mismatch (a forged / stale
    checkpoint) is rejected (no fork)."""
    if not isinstance(cp, dict):
        return False
    obs = cp.get("observation_id")
    ph = cp.get("prefix_hash")
    rh = cp.get("repo_state_hash")
    cid = cp.get("checkpoint_id")
    if not (isinstance(obs, int) and not isinstance(obs, bool) and obs >= 0):
        return False
    if not (isinstance(ph, str) and ph.strip()):
        return False
    if not (isinstance(rh, str) and rh.strip()):
        return False
    if not (isinstance(cid, str) and cid.strip()):
        return False
    expected = hashlib.sha256(_canonical([obs, ph, rh])).hexdigest()
    return cid == expected


def _fork_citation_valid(cit: Any) -> bool:
    """The citation anchor must name the exact model-visible observation the safety decision
    cites and a well-formed char span. A malformed / forged anchor is rejected (no fork)."""
    if not isinstance(cit, dict):
        return False
    obs = cit.get("observation_id")
    span = cit.get("char_span")
    if not (isinstance(obs, int) and not isinstance(obs, bool) and obs >= 0):
        return False
    if not (isinstance(span, (list, tuple)) and len(span) == 2):
        return False
    a, b = span
    if not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (a, b)):
        return False
    return a <= b


def _rederive_fork_signals(
    fact_class: str, producer_inputs: Any
) -> tuple[str, str, bool] | None:
    """PURELY re-derive the treatment (GT candidate present) and control (GT candidate withheld
    from the model-visible surface) signals for one safety decision, via the SAME pure producer
    the seam uses. Returns ``(treatment_signal, control_signal, differ_causally)`` or ``None``
    when the inputs are malformed. NO real submission, NO real mutation — pure function calls."""
    if not isinstance(producer_inputs, dict):
        return None
    treatment = producer_inputs.get("treatment")
    control = producer_inputs.get("control")
    if not isinstance(treatment, dict) or not isinstance(control, dict):
        return None

    if fact_class == "submit_refusal":
        # the submit gate's PURE kernel: a BLOCK is a refusal, an ALLOW is a proceed. The control
        # arm withholds the GT covering candidate (typically covering=None) from the gate.
        from groundtruth.runtime.submit_gate import gate_verdict

        def _sig(args: dict) -> str:
            gv = gate_verdict(
                covering=args.get("covering"),
                hygiene=args.get("hygiene"),
                bounce_count=int(args.get("bounce_count", 0) or 0),
                max_bounces=int(args.get("max_bounces", 1) or 1),
            )
            return "ALLOW" if gv.allow else "BLOCK"

        ts, cs = _sig(treatment), _sig(control)
        # causal iff GT's presence FLIPPED the safety decision to a refusal that the withheld
        # arm would have let proceed.
        return ts, cs, (ts == "BLOCK" and cs == "ALLOW")

    if fact_class == "syntax_result":
        # the lane's PURE syntax attestation: a COMPLETE attestation (truth PASS) is a real,
        # bound syntax RED. The control arm withholds the GT candidate from the model-visible
        # surface, so its attestation is incomplete (truth not PASS).
        from groundtruth.runtime.lane_attestation import finalize_syntax_attestation
        from groundtruth.runtime.producer_attestation import PASS as _ATTEST_PASS

        def _truth(args: dict) -> str | None:
            try:
                final = finalize_syntax_attestation(**args)
            except Exception:  # noqa: BLE001 — malformed producer input -> unmeasurable
                return None
            preds = getattr(final.attestation, "truth_predicates", ()) or ()
            if not preds:
                return None
            return getattr(preds[0], "verdict", None)

        tv, cv = _truth(treatment), _truth(control)
        if tv is None:
            return None
        ts = "PASS" if tv == _ATTEST_PASS else "UNMEASURED"
        cs = "PASS" if cv == _ATTEST_PASS else "UNMEASURED"
        # causal iff the treatment RED is REAL (PASS) while the withheld control is not.
        return ts, cs, (ts == "PASS" and cs != "PASS")

    return None


def _safety_fork_probes(safety_forks: Any) -> list[ProbeResult]:
    """B4: mint a DISTINCT ``CAUSAL_FORK`` per caller-supplied sealed safety-decision checkpoint.

    Each fork is a PURE re-derivation over a sealed checkpoint: the checkpoint identity is
    VERIFIED (``sha256(observation_id||prefix_hash||repo_state_hash)``), the citation anchor is
    VERIFIED, and the treatment (GT candidate present) vs control (GT candidate withheld) signals
    are re-derived through the pure producer (``gate_verdict`` / ``finalize_syntax_attestation``).
    A verdict is minted ONLY when the two arms DIFFER causally — instrument presence alone is
    NEVER a verdict. NO real submission, NO real mutation, NO episode branch. Four sealed
    artifacts + a citation anchor are recorded; the confidence is explicitly bounded (N=1)."""
    if not safety_forks:
        return []
    results: list[ProbeResult] = []
    for fork in safety_forks:
        if not isinstance(fork, dict):
            continue
        fc = fork.get("fact_class")
        if fc not in SAFETY_EXCLUDED_CLASSES:
            continue
        seal = fork.get("delivery_seal")
        if not _valid_seal(seal):
            continue
        cp = fork.get("checkpoint")
        cit = fork.get("citation")
        if not _fork_checkpoint_valid(cp):
            continue  # mismatched checkpoint identity rejected
        if not _fork_citation_valid(cit):
            continue  # forged citation anchor rejected
        # the validators proved both are well-formed dicts; narrow for the type checker.
        assert isinstance(cp, dict) and isinstance(cit, dict)
        signals = _rederive_fork_signals(fc, fork.get("producer_inputs"))
        if signals is None:
            continue
        treatment_signal, control_signal, causal = signals
        if not causal:
            # a non-differing re-derivation is NOT a verdict (instrument presence never a verdict).
            continue

        checkpoint_id = cp["checkpoint_id"]
        assignment_artifact = {
            "schema": "gt.fair_probe.fork_assignment.v1",
            "fact_class": fc,
            "assignment_unit_id": checkpoint_id,
            "treatment_seal": seal,
            "checkpoint_id": checkpoint_id,
            "observation_id": cp["observation_id"],
        }
        treatment_artifact = {
            "schema": "gt.fair_probe.fork_treatment.v1",
            "seal": seal,
            "signal": treatment_signal,
            "candidate_withheld": False,
        }
        control_artifact = {
            "schema": "gt.fair_probe.fork_control.v1",
            "signal": control_signal,
            "candidate_withheld": True,
        }
        citation_anchor = {
            "schema": "gt.fair_probe.fork_citation.v1",
            "observation_id": cit["observation_id"],
            "char_span": [int(cit["char_span"][0]), int(cit["char_span"][1])],
        }
        outcome_artifact = {
            "schema": "gt.fair_probe.fork_outcome.v1",
            "treatment_signal": treatment_signal,
            "control_signal": control_signal,
            "differ": True,
            "verdict": CAUSAL_FORK,
            "bounded_confidence": "N1_checkpoint",
        }
        probe = MatchedProbe(
            probe_id=f"fork:{fc}:{seal}:{checkpoint_id}",
            assignment_unit_id=checkpoint_id,
            treatment_seal=str(seal),
            treatment_outcome="acted",
            control_outcome="not_acted",
            assignment_sha256=_sha256(assignment_artifact),
            treatment_sha256=_sha256(treatment_artifact),
            control_sha256=_sha256(control_artifact),
            outcome_sha256=_sha256(outcome_artifact),
        )
        artifacts = {
            "assignment": assignment_artifact,
            "treatment": treatment_artifact,
            "control": control_artifact,
            "citation_anchor": citation_anchor,
            "outcome": outcome_artifact,
        }
        results.append(
            ProbeResult(
                probe_kind="safety_fork",
                fact_class=str(fc),
                delivered_row_index=None,
                holdout_row_index=None,
                treatment_seal=str(seal),
                control_seal="",
                treatment_outcome="acted",
                control_outcome="not_acted",
                causal_verdict=CAUSAL_FORK,
                matched_probe=probe,
                artifacts=artifacts,
                bounded_confidence="N1_checkpoint",
            )
        )
    return results


def _merge_verdict(current: str | None, incoming: str) -> str:
    if current is None:
        return incoming
    return incoming if _VERDICT_RANK.get(incoming, 0) > _VERDICT_RANK.get(current, 0) else current


def _merge_gate(current: bool | None, incoming: bool | None) -> bool | None:
    """Roll up the fair-probe GATE across every verdict contributing to one fact class.

    Gate precedence is by STRENGTH OF PROOF, NOT by ``_VERDICT_RANK`` (which orders the reported
    ``verdict`` headline): a proven behavioural ``CAUSAL`` (True) wins; else a proven
    ``SELF_LOCALIZED`` (False, an honest non-causal) wins; else ``None`` (unmeasured). A
    mechanism-only / enrichment verdict (``CAUSAL_FORK`` / ``CAUSAL_PAIRED`` — both gate ``None``
    by B5) therefore can NEVER MASK a co-present derivable ``SELF_LOCALIZED`` / ``CAUSAL`` down to
    ``None``: it only contributes ``None``, which loses to any concrete bool. This never mints a
    ``True`` without a ``CAUSAL`` adjudication (no bar weakening) — it only stops a derivable
    verdict being dropped to ``None`` when a non-gating verdict happens to out-rank it."""
    if current is True or incoming is True:
        return True
    if current is False or incoming is False:
        return False
    return None


def _verdict_to_bool(verdict: str) -> bool | None:
    """The behavioural fair-probe gate.

    Only an agent-behaviour ``CAUSAL`` adjudication can set it. ``CAUSAL_FORK`` proves a
    deterministic safety mechanism changed its own decision, not that the agent behaved better.
    ``CAUSAL_PAIRED`` remains outcome enrichment. Both stay unmeasured for this gate.
    """
    if verdict == CAUSAL:
        return True
    if verdict == SELF_LOCALIZED:
        return False
    return None


def _window_consequences(
    messages: list[dict], lo: int, hi: int
) -> dict[str, float]:
    """Measure REAL-VALUED proximal-efficiency consequences over the message window
    ``messages[lo:hi]`` — the span from an opportunity boundary to the next decision commit.

    The window is the same message-index space every J3 derivation uses.  Each assistant
    command-bearing message is one step; its commands are classified with the SAME
    ``_action_kind`` the consumption ledger uses (mutation / verification / else), so ``edit_cycles``
    and ``verification_runs`` mean exactly what the receipt grader means, and any other command
    (grep / cat / read / search) is a ``native_search_view``.  Bounded and empty-safe: an
    out-of-range or inverted window yields all-zero counts and never raises.  Values are floats
    so they feed :func:`live_causal_cohort.evaluate_live_cohort` as a continuous endpoint Y."""
    lo = max(0, lo)
    hi = min(len(messages), hi)
    steps = views = edits = verifications = 0
    for j in range(lo, hi):
        msg = messages[j]
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        commands = _emitted_commands(msg)
        if not commands:
            continue
        steps += 1
        for cmd in commands:
            kind = _action_kind(cmd)
            if kind == "mutation":
                edits += 1
            elif kind == "verification":
                verifications += 1
            else:
                views += 1
    return {
        "steps_to_next_decision": float(steps),
        "native_search_views": float(views),
        "edit_cycles": float(edits),
        "verification_runs": float(verifications),
    }


def extract_live_opportunity_observations(
    trajectory: Any,
    ledger_rows: list[dict],
    chronologies: dict[int, ExtractedChronology],
    *,
    task_label: str,
    live_witness: bool,
    attestations: Any = None,
) -> list[dict[str, Any]]:
    """Export one intention-to-treat endpoint per durable shadow assignment.

    The assignment and outcome are joined only by the immutable observation opportunity,
    canonical candidate, canonical fact class, and exact candidate seal.  Ledger row order is
    the pre-outcome chronology coordinate.  DELIVER uses the registered per-row receipt
    predicate; HOLDOUT evaluates the same predicate over the control consequence window.
    Malformed, censored, duplicated, or pre-acquired opportunities remain in the export as
    unmeasured/contaminated records so a cohort analyzer cannot silently select them away.
    """
    messages = _messages(trajectory)
    tool_ordinal_index = _tool_ordinal_to_index(messages)
    boundaries = _boundary_indices(messages)
    flat_boundaries = sorted({i for lst in boundaries.values() for i in lst})
    assignments = [
        (index, row)
        for index, row in enumerate(ledger_rows)
        if isinstance(row, dict)
        and row.get("schema") == "gt.shadow_assignment.v1"
        and row.get("layer") == "shadow.assignment"
        and row.get("outcome") == "assigned"
    ]
    exported: list[dict[str, Any]] = []
    for assignment_index, assignment_row in assignments:
        opportunity_id = _opportunity_id(assignment_row)
        assignment = assignment_row.get("assignment")
        candidate_id = assignment_row.get("candidate_id")
        fact_class = canonical_class(str(assignment_row.get("fact_class") or ""))
        candidate_seal = assignment_row.get("candidate_sha256_16")
        probability = assignment_row.get("assignment_probability")
        failures: list[str] = []
        if opportunity_id is None:
            failures.append("invalid_opportunity_id")
        if assignment not in {"DELIVER", "HOLDOUT"}:
            failures.append("invalid_assignment")
        if not isinstance(candidate_id, str) or not candidate_id:
            failures.append("invalid_candidate_id")
        if fact_class is None:
            failures.append("invalid_fact_class")
        if not _valid_seal(candidate_seal):
            failures.append("invalid_candidate_seal")
        if (
            not isinstance(probability, (int, float))
            or isinstance(probability, bool)
            or not 0.0 < float(probability) < 1.0
        ):
            failures.append("invalid_assignment_probability")

        outcomes: list[tuple[int, dict]] = []
        if opportunity_id is not None:
            for outcome_index, row in enumerate(ledger_rows):
                if outcome_index <= assignment_index or not isinstance(row, dict):
                    continue
                if row.get("outcome") not in {"delivered", "shadow_holdout"}:
                    continue
                if _opportunity_id(row) != opportunity_id:
                    continue
                if row.get("candidate_id") != candidate_id:
                    continue
                if _holdout_class(row) != fact_class:
                    continue
                if row.get("content_sha256_16") != candidate_seal:
                    continue
                outcomes.append((outcome_index, row))
        expected_outcome = "delivered" if assignment == "DELIVER" else "shadow_holdout"
        matching = [
            item for item in outcomes if item[1].get("outcome") == expected_outcome
        ]
        opposite = [
            item for item in outcomes if item[1].get("outcome") != expected_outcome
        ]
        if len(matching) != 1:
            failures.append("outcome_join_not_unique")
        if opposite:
            failures.append("opposite_arm_outcome_present")

        outcome_index: int | None = matching[0][0] if len(matching) == 1 else None
        endpoint: bool | None = None
        contaminated = bool(opposite)
        # CONTINUOUS proximal endpoints: measured over the window from the opportunity boundary
        # to the next decision commit (DELIVER) / the next observation boundary (HOLDOUT). These
        # are the intention-to-treat efficiency outcomes a cohort projects with
        # ``project_continuous_endpoint``; the boolean receipt below is unchanged.
        consequences = _window_consequences(messages, 0, 0)
        if outcome_index is not None and fact_class is not None:
            if assignment == "DELIVER":
                chronology = chronologies.get(outcome_index)
                if chronology is None:
                    failures.append("delivery_chronology_missing")
                else:
                    endpoint = receipt_predicates.acknowledgment_for_row(
                        chronology,
                        messages=messages,
                        ledger_rows=ledger_rows,
                        attestations=attestations,
                    )
                    native_index = getattr(
                        getattr(chronology, "chronology", None),
                        "native_acquisition_index",
                        None,
                    )
                    if native_index is not None:
                        contaminated = True
                        failures.append("pre_delivery_reacquisition")
                    inner = getattr(chronology, "chronology", None)
                    delivery_index = getattr(inner, "delivery_index", None)
                    commit_index = getattr(inner, "decision_commit_index", None)
                    if isinstance(delivery_index, int) and not isinstance(delivery_index, bool):
                        if (
                            isinstance(commit_index, int)
                            and not isinstance(commit_index, bool)
                            and commit_index > delivery_index
                        ):
                            window_hi = commit_index + 1  # window is inclusive of the commit
                        else:
                            window_hi = next(
                                (b for b in flat_boundaries if b > delivery_index),
                                len(messages),
                            )
                        consequences = _window_consequences(
                            messages, delivery_index + 1, window_hi
                        )
            else:
                control = _control_outcome(
                    matching[0][1],
                    outcome_index,
                    fact_class,
                    messages,
                    tool_ordinal_index,
                    boundaries,
                    ledger_rows,
                    attestations,
                )
                endpoint = control.receipt
                if control.withhold_index is None:
                    failures.append("withhold_boundary_missing")
                else:
                    file_path = str(matching[0][1].get("file_path") or "")
                    if _native_acquisition_index(
                        messages, control.withhold_index, "", file_path
                    ) is not None:
                        contaminated = True
                        failures.append("pre_holdout_reacquisition")
                    window_hi = (
                        control.window_end
                        if isinstance(control.window_end, int)
                        else len(messages)
                    )
                    consequences = _window_consequences(
                        messages, control.withhold_index + 1, window_hi
                    )
                if control.outcome == "unresolved":
                    failures.append("control_consequence_unresolved")
        if endpoint is None:
            failures.append("endpoint_unmeasured")

        exported.append({
            "schema": "gt.live_opportunity_observation.v1",
            "task_id": task_label,
            "opportunity_id": opportunity_id or "",
            "fact_class": fact_class or "",
            "candidate_id": candidate_id if isinstance(candidate_id, str) else "",
            "candidate_sha256_16": (
                candidate_seal if isinstance(candidate_seal, str) else ""
            ),
            "assignment": assignment if isinstance(assignment, str) else "",
            "assignment_probability": (
                float(probability)
                if isinstance(probability, (int, float))
                and not isinstance(probability, bool)
                else None
            ),
            "endpoint": endpoint,
            "endpoint_name": "registered_receipt",
            "consequences": consequences,
            "live_witness": bool(live_witness),
            "assignment_index": assignment_index,
            "outcome_index": outcome_index,
            "contaminated": contaminated,
            "failures": sorted(set(failures)),
        })
    return exported


def _write_sidecar(output_dir: str, task_label: str, document: dict[str, Any]) -> str | None:
    """Write the sealed result sidecar to the designated output dir (deterministic bytes).
    Fail-closed: any fault -> no path (never breaks the grader)."""
    try:
        safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in (task_label or "task"))
        path = os.path.join(output_dir, f"gt_fair_probe_results_{safe}.json")
        with open(path, "wb") as handle:
            handle.write(_canonical(document))
        return path
    except Exception:  # noqa: BLE001 - a sidecar write must never break the grader
        return None


def join_fair_probes(
    trajectory: Any,
    ledger_rows: list[dict],
    *,
    chronologies: dict[int, ExtractedChronology] | None = None,
    output_dir: str | None = None,
    task_label: str = "",
    live_witness: bool = False,
    gt_resolved: bool | None = None,
    baseline_resolved: bool | None = None,
    attestations: Any = None,
    safety_forks: Any = None,
) -> dict[str, Any]:
    """Adjudicate every fair probe and roll up per canonical fact class.

    ``per_fact_class`` carries each class's fair-probe verdict + its bool gate value for
    ``ss_gate_readiness(fair_probe=...)``: True only for behavioral CAUSAL evidence;
    a safety CAUSAL_FORK remains mechanism evidence; SELF_LOCALIZED (False) when chronology proves prior
    self-acquisition; CAUSAL_PAIRED (ENRICHMENT ONLY, gate None) on the safety paired-baseline
    path; else UNMEASURED. ``probes`` is the per-probe audit trail and
    ``opportunity_observations`` is the live-cohort input. Same-trajectory distinct candidates
    are diagnostic only; the fork verdict is a pure re-derivation over a sealed checkpoint.
    ``attestations`` feed the B2/B3 covering receipt; ``safety_forks`` are caller-supplied sealed
    B4 checkpoints (absent -> the fork path is dormant, fail-closed)."""
    if chronologies is None:
        chronologies = extract_chronologies(trajectory, ledger_rows)

    messages = _messages(trajectory)

    randomized = compute_matched_probes(
        trajectory, ledger_rows, chronologies, attestations=attestations
    )
    paired = _paired_baseline_probes(
        chronologies, gt_resolved, baseline_resolved,
        messages=messages, ledger_rows=ledger_rows, attestations=attestations,
    )
    forks = _safety_fork_probes(safety_forks)
    all_probes = randomized + paired + forks

    verdict_by_class: dict[str, str] = {}
    # The fair-probe GATE, rolled up SEPARATELY from the reported ``verdict`` headline: a
    # non-gating (mechanism-only ``CAUSAL_FORK`` / enrichment ``CAUSAL_PAIRED``) verdict that
    # out-ranks a co-present derivable ``SELF_LOCALIZED`` / ``CAUSAL`` for the HEADLINE must NOT
    # drop the gate to ``None`` — the gate is the strongest PROVEN behavioural bool (see
    # ``_merge_gate``). Fixes the drop-to-None where the join carried a derivable verdict.
    gate_by_class: dict[str, bool | None] = {}

    def _record(canon: str, verdict: str) -> None:
        verdict_by_class[canon] = _merge_verdict(verdict_by_class.get(canon), verdict)
        gate_by_class[canon] = _merge_gate(
            gate_by_class.get(canon), _verdict_to_bool(verdict)
        )

    # randomized probes carry the adjudicated CAUSAL / SELF_LOCALIZED / UNMEASURED verdict; the
    # paired/fork probes carry their own explicit verdict.
    for probe in all_probes:
        _record(probe.fact_class, probe.causal_verdict)

    # every delivered row also contributes its no-probe adjudication (SELF_LOCALIZED /
    # UNMEASURED) so a class with prior self-acquisition and no holdout is graded honestly.
    for row_index in sorted(chronologies):
        ec = chronologies[row_index]
        canon = canonical_class(ec.fact_class or ec.evidence_type or "")
        if canon is None:
            continue
        if not _valid_seal(ec.delivery_seal):
            _record(canon, UNMEASURED)
            continue
        verdict = adjudicate(
            evidence_type=ec.evidence_type,
            actual_event=ec.actual_event,
            delivery_seal=ec.delivery_seal,
            chronology=ec.chronology,
            matched_probe=None,
        ).fair_probe_verdict
        _record(canon, verdict)

    per_fact_class: dict[str, Any] = {}
    for fact_class, verdict in sorted(verdict_by_class.items()):
        class_probes = [p for p in all_probes if p.fact_class == fact_class]
        per_fact_class[fact_class] = {
            "verdict": verdict,
            "fair_probe": gate_by_class.get(fact_class),
            "randomized_probes": sum(1 for p in class_probes if p.probe_kind == "randomized"),
            "paired_baseline_probes": sum(
                1 for p in class_probes if p.probe_kind == "paired_baseline"
            ),
            "safety_fork_probes": sum(1 for p in class_probes if p.probe_kind == "safety_fork"),
            "causal_probes": sum(1 for p in class_probes if p.causal_verdict == CAUSAL),
            "causal_fork_probes": sum(
                1 for p in class_probes if p.causal_verdict == CAUSAL_FORK
            ),
        }

    opportunity_observations = extract_live_opportunity_observations(
        trajectory,
        ledger_rows,
        chronologies,
        task_label=task_label,
        live_witness=live_witness,
        attestations=attestations,
    )
    document: dict[str, Any] = {
        "schema": FAIR_PROBE_RESULT_SCHEMA,
        "per_fact_class": per_fact_class,
        "probes": [p.to_dict() for p in all_probes],
        "opportunity_observations": opportunity_observations,
    }
    sidecar_path = (
        _write_sidecar(output_dir, task_label, document) if output_dir else None
    )
    document["sidecar_path"] = sidecar_path
    return document


def fair_probe_bool_by_fact_class(join: dict[str, Any]) -> dict[str, bool | None]:
    """The ``fact_class -> fair_probe`` gate map the grader threads into ``ss_gate_readiness``:
    True only for behavioral CAUSAL, False for SELF_LOCALIZED, and None for UNMEASURED,
    mechanism-only CAUSAL_FORK, and enrichment-only CAUSAL_PAIRED."""
    out: dict[str, bool | None] = {}
    for fact_class, info in (join.get("per_fact_class") or {}).items():
        if isinstance(info, dict):
            out[fact_class] = info.get("fair_probe")
    return out


__all__ = [
    "CAUSAL_FORK",
    "CAUSAL_PAIRED",
    "FAIR_PROBE_RESULT_SCHEMA",
    "ControlResult",
    "ProbeResult",
    "compute_matched_probes",
    "extract_live_opportunity_observations",
    "fair_probe_bool_by_fact_class",
    "join_fair_probes",
]
