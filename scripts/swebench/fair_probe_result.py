#!/usr/bin/env python3
"""SPEC-J4 — the fair-probe RESULT writer: shadow-holdout ledger rows + the J3 chronology
-> seal-bound ``MatchedProbe`` artifacts, adjudicated through the CAUSAL authority.

THE PROBLEM. Gate 7 (fair causal probe) is ``None`` for every ACQ/FACT row. The authority
(``groundtruth.runtime.chronological_adjudication.adjudicate``) already exists and is
fail-closed: it returns CAUSAL only for an ON_TIME delivery with a valid seal-bound
``MatchedProbe`` (``treatment_seal == delivery_seal``, ``treatment_outcome=="acted"``,
``control_outcome=="not_acted"``, four sha256-sealed artifacts) plus post-delivery ack+action.
The shadow-holdout INSTRUMENT already exists (the seam writes ``outcome=="shadow_holdout"``
rows carrying the withheld render's ``content_sha256_16`` / ``fact_class`` / ``dedup_key`` —
gt_mini_patch.py ~15556 and ~14917). What did NOT exist is the RESULT computation that turns
holdout rows + trajectory into ``MatchedProbe`` artifacts, and the grader join. This module is
that computation. **Instrument presence is NEVER a verdict** — that fabrication was reverted
(ea0eb16c0); a verdict flows ONLY through ``adjudicate``.

REUSE, NOT REINVENT (the reader/writer-mismatch discipline). Every join reuses the landed J3
machinery and the consumption-ledger entity model the grader already trusts:

* the TREATMENT arm is read straight from J3's ``extract_chronologies`` output — this module
  never re-derives the six indices. ``treatment_outcome=="acted"`` iff the delivered row's
  chronology shows an acknowledgment AND an action strictly after delivery (the same two
  indices ``adjudicate`` re-checks for CAUSAL — defence in depth, no divergent second join).
* the CONTROL arm reads the withheld instance from its shadow-holdout row. The withheld bytes
  NEVER reach the grader (leak-safe: only the row's HASH + ``file_path`` survive), so the
  control's entity anchor is the row's ``file_path`` — exactly the fallback J3's
  ``_native_acquisition_index`` / ``_action_index`` use when a payload is unavailable. The
  entity matchers are ``consumption_ledger._entity_patterns`` / ``_named_in`` (the SAME
  grader-side model J3 reuses; the runtime ``_ss_entity_set`` extractor is not importable into
  a pure grader without pulling the seam's whole runtime). ``control_outcome=="not_acted"``
  iff the agent named NO withheld entity in any command between the withholding point and the
  NEXT observation boundary — i.e. the agent did NOT self-acquire the withheld fact inside the
  decision window. If the agent acted anyway -> ``control_outcome=="acted"`` -> the probe is
  INVALID for CAUSAL (honest: GT was not needed).
* the message-index space + observation boundaries + tool-ordinal map are reused verbatim from
  ``chronology_extract`` (``_messages`` / ``_tool_ordinal_to_index`` / ``_boundary_indices``) —
  there is exactly ONE join, J3's.

PAIRED-BASELINE PATH (safety-excluded classes only). ``syntax_result`` / ``submit_refusal``
(``shadow_holdout.SAFETY_EXCLUDED_CLASSES``) can NEVER have a holdout row by design — withholding
a safety check could corrupt the episode. Their causal claim therefore rides an OPTIONAL paired
baseline verdict passed by the CALLER (never read from disk here): when the GT-on run resolved
and the baseline did not, and the class delivered ON_TIME with ack+action, a ``paired_baseline``
result is emitted with verdict ``CAUSAL_PAIRED`` — a DISTINCT schema field, never conflated with
the randomized CAUSAL. Absent baseline input -> UNMEASURED.

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
    MatchedProbe,
    adjudicate,
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
    _messages,
    _tool_ordinal_to_index,
    _valid_seal,
    extract_chronologies,
)

# REUSE the grader-side entity model (the SAME one J3 uses for native acquisition / action).
from consumption_ledger import _emitted_commands, _entity_patterns, _named_in

# The adjudicator's own schema string is the result schema (this is the adjudicated result).
FAIR_PROBE_RESULT_SCHEMA = "gt.fair_probe_result.v1"

# The paired-baseline verdict — DISTINCT from the randomized CAUSAL, never conflated. The
# adjudicator has no concept of it (it has no randomized control), so it is set here directly.
CAUSAL_PAIRED = "CAUSAL_PAIRED"

# verdict precedence for the per-class rollup (CAUSAL / CAUSAL_PAIRED win; UNMEASURED loses).
_VERDICT_RANK = {
    UNMEASURED: 1,
    SELF_LOCALIZED: 2,
    CAUSAL: 4,
    CAUSAL_PAIRED: 4,
}


def _canonical(value: object) -> bytes:
    """The adjudicator's canonical-JSON encoding (byte-identical, deterministic)."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


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
    outcome: str  # "not_acted" | "acted" | "unresolved"


@dataclass(frozen=True)
class ProbeResult:
    """One adjudicated fair-probe result (randomized or paired-baseline)."""

    probe_kind: str  # "randomized" | "paired_baseline"
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
            "matched_probe": (
                asdict(self.matched_probe) if self.matched_probe is not None else None
            ),
            "artifacts": self.artifacts,
        }


# --------------------------------------------------------------------------- #
# arm computations
# --------------------------------------------------------------------------- #
def _treatment_acted(chronology) -> bool:
    """The delivered instance was consumed: an acknowledgment AND an action BOTH strictly after
    delivery (the exact two indices ``adjudicate`` re-checks for CAUSAL)."""
    delivered = chronology.delivery_index
    ack = chronology.acknowledgment_index
    act = chronology.action_index
    return bool(
        isinstance(delivered, int)
        and isinstance(ack, int)
        and isinstance(act, int)
        and ack > delivered
        and act > delivered
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


def _control_outcome(
    row: dict,
    row_index: int,
    fact_class: str,
    messages: list[dict],
    tool_ordinal_index: dict[int, int],
    boundary_indices: list[int],
) -> ControlResult:
    """Grade the withheld arm. ``not_acted`` iff the withholding point is located AND a valid
    withheld-render hash is present AND the withheld entity resolves AND the agent INSPECTED at
    least one decision in the window (an assistant command-bearing message between the withholding
    point and the NEXT observation boundary) yet named NO withheld entity in it. ``acted`` iff a
    command in that window names the entity (the agent self-acquired the fact anyway -> GT not
    needed). ``unresolved`` (fail-closed, ``_valid_probe`` rejects it -> never CAUSAL) when the
    withholding point / hash / entity cannot be located OR the window held ZERO agent decisions.

    That last clause is load-bearing: an EMPTY window (withholding at end-of-trajectory, or a
    boundary adjacent to the withholding point) means the agent made NO decision GT could have
    changed — an absence of activity is NOT evidence the agent 'did not self-acquire'. Reading it
    as ``not_acted`` would mint a false CAUSAL from censoring (the ea0eb16c0 fabrication class on
    the terminal gate); it is ``unresolved`` instead."""
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

    # The shadow instrument ALWAYS seals the withheld render (shadow_holdout invariant 5); a
    # hash-less row is malformed -> unresolved (a control arm without a valid withheld-hash is
    # never accepted into a randomized probe).
    if withhold_index is None or not pats or not _valid_seal(control_seal):
        return ControlResult(
            holdout_row_index=row_index, fact_class=fact_class, control_seal=control_seal,
            withhold_index=withhold_index, window_end=None, entity_named=None,
            outcome="unresolved",
        )

    # the decision window closes at the NEXT observation boundary after the withholding point.
    window_end = next((b for b in boundary_indices if b > withhold_index), len(messages))
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
        if entity_named:
            break

    if entity_named:
        outcome = "acted"
    elif inspected_decision:
        outcome = "not_acted"
    else:
        # empty / activity-free window: no agent decision to be a counterfactual over.
        outcome = "unresolved"

    return ControlResult(
        holdout_row_index=row_index, fact_class=fact_class, control_seal=control_seal,
        withhold_index=withhold_index, window_end=window_end, entity_named=entity_named,
        outcome=outcome,
    )


def _build_matched_probe(
    ec: ExtractedChronology,
    fact_class: str,
    treatment_outcome: str,
    control: ControlResult,
) -> tuple[MatchedProbe, dict[str, Any]]:
    """Seal the four canonical artifacts (assignment, treatment, control, outcome) and bind them
    into a ``MatchedProbe``. The treatment seal IS the delivery seal (the doctrine's seal-bind)."""
    assignment_artifact = {
        "schema": "gt.fair_probe.assignment.v1",
        "fact_class": fact_class,
        "assignment_unit_id": fact_class,
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
        "outcome": treatment_outcome,
    }
    control_artifact = {
        "schema": "gt.fair_probe.control.v1",
        "seal": control.control_seal,
        "withhold_index": control.withhold_index,
        "window_end": control.window_end,
        "entity_named": control.entity_named,
        "outcome": control.outcome,
    }
    outcome_artifact = {
        "schema": "gt.fair_probe.outcome.v1",
        "treatment_outcome": treatment_outcome,
        "control_outcome": control.outcome,
        "differ": treatment_outcome != control.outcome,
    }
    probe = MatchedProbe(
        probe_id=f"{fact_class}:{ec.delivery_seal}:{control.control_seal}",
        assignment_unit_id=fact_class,
        treatment_seal=ec.delivery_seal,
        treatment_outcome=treatment_outcome,
        control_outcome=control.outcome,
        assignment_sha256=_sha256(assignment_artifact),
        treatment_sha256=_sha256(treatment_artifact),
        control_sha256=_sha256(control_artifact),
        outcome_sha256=_sha256(outcome_artifact),
    )
    artifacts = {
        "assignment": assignment_artifact,
        "treatment": treatment_artifact,
        "control": control_artifact,
        "outcome": outcome_artifact,
    }
    return probe, artifacts


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def compute_matched_probes(
    trajectory: Any,
    ledger_rows: list[dict],
    chronologies: dict[int, ExtractedChronology],
) -> list[ProbeResult]:
    """Build one adjudicated ``ProbeResult`` per (delivered row, same-class holdout row) pair.

    The treatment arm is J3's chronology for the delivered row; the control arm is the withheld
    instance graded by :func:`_control_outcome`. The CAUSAL verdict flows ONLY through
    ``adjudicate`` with the seal-bound ``MatchedProbe``. Deterministic order: by
    (delivered_row_index, holdout_row_index)."""
    messages = _messages(trajectory)
    tool_ordinal_index = _tool_ordinal_to_index(messages)
    boundaries = _boundary_indices(messages)
    boundary_indices = sorted({i for lst in boundaries.values() for i in lst})

    holdouts_by_class: dict[str, list[ControlResult]] = {}
    for row_index, row in enumerate(ledger_rows):
        if not isinstance(row, dict) or row.get("outcome") != "shadow_holdout":
            continue
        canon = _holdout_class(row)
        if canon is None:
            continue
        control = _control_outcome(
            row, row_index, canon, messages, tool_ordinal_index, boundary_indices
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
        treatment_outcome = "acted" if _treatment_acted(ec.chronology) else "not_acted"
        for control in sorted(controls, key=lambda c: c.holdout_row_index):
            probe, artifacts = _build_matched_probe(
                ec, canon, treatment_outcome, control
            )
            verdict = adjudicate(
                evidence_type=ec.evidence_type,
                actual_event=ec.actual_event,
                delivery_seal=ec.delivery_seal,
                chronology=ec.chronology,
                matched_probe=probe,
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
) -> list[ProbeResult]:
    """The paired-baseline path for SAFETY-excluded classes (no holdout possible). Fires ONLY
    when the caller supplies ``gt_resolved is True`` and ``baseline_resolved is False`` (a real
    GT-on lift over baseline) AND the class delivered ON_TIME with ack+action. Absent / weaker
    input -> no probe (UNMEASURED). A same-row SELF_LOCALIZED chronology (the agent self-acquired
    the fact before GT delivered it) is NEVER upgraded to CAUSAL_PAIRED — GT was not the cause."""
    if gt_resolved is not True or baseline_resolved is not False:
        return []
    results: list[ProbeResult] = []
    for row_index in sorted(chronologies):
        ec = chronologies[row_index]
        canon = canonical_class(ec.fact_class or ec.evidence_type or "")
        if canon is None or canon not in SAFETY_EXCLUDED_CLASSES:
            continue
        if ec.timing_verdict != ON_TIME or not _treatment_acted(ec.chronology):
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


def _merge_verdict(current: str | None, incoming: str) -> str:
    if current is None:
        return incoming
    return incoming if _VERDICT_RANK.get(incoming, 0) > _VERDICT_RANK.get(current, 0) else current


def _verdict_to_bool(verdict: str) -> bool | None:
    """The fair_probe gate value: True for a proven causal result (randomized or paired), False
    when the agent self-localized, None when unmeasured (fail-closed)."""
    if verdict in (CAUSAL, CAUSAL_PAIRED):
        return True
    if verdict == SELF_LOCALIZED:
        return False
    return None


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
    gt_resolved: bool | None = None,
    baseline_resolved: bool | None = None,
) -> dict[str, Any]:
    """Adjudicate every fair probe and roll up per canonical fact class.

    ``per_fact_class`` carries each class's fair-probe verdict + its bool gate value for
    ``ss_gate_readiness(fair_probe=...)``: CAUSAL only when at least one row adjudicates CAUSAL;
    SELF_LOCALIZED when the chronology proves prior self-acquisition; CAUSAL_PAIRED on the
    safety-excluded paired-baseline path; else UNMEASURED. ``probes`` is the per-probe audit
    trail. The randomized causal verdict flows ONLY through ``adjudicate``; the paired verdict
    takes the baseline verdict as an INPUT (never read from disk here)."""
    if chronologies is None:
        chronologies = extract_chronologies(trajectory, ledger_rows)

    randomized = compute_matched_probes(trajectory, ledger_rows, chronologies)
    paired = _paired_baseline_probes(chronologies, gt_resolved, baseline_resolved)
    all_probes = randomized + paired

    verdict_by_class: dict[str, str] = {}

    # randomized probes carry the adjudicated CAUSAL / SELF_LOCALIZED / UNMEASURED verdict.
    for probe in all_probes:
        verdict_by_class[probe.fact_class] = _merge_verdict(
            verdict_by_class.get(probe.fact_class), probe.causal_verdict
        )

    # every delivered row also contributes its no-probe adjudication (SELF_LOCALIZED /
    # UNMEASURED) so a class with prior self-acquisition and no holdout is graded honestly.
    for row_index in sorted(chronologies):
        ec = chronologies[row_index]
        canon = canonical_class(ec.fact_class or ec.evidence_type or "")
        if canon is None:
            continue
        if not _valid_seal(ec.delivery_seal):
            verdict_by_class[canon] = _merge_verdict(verdict_by_class.get(canon), UNMEASURED)
            continue
        verdict = adjudicate(
            evidence_type=ec.evidence_type,
            actual_event=ec.actual_event,
            delivery_seal=ec.delivery_seal,
            chronology=ec.chronology,
            matched_probe=None,
        ).fair_probe_verdict
        verdict_by_class[canon] = _merge_verdict(verdict_by_class.get(canon), verdict)

    per_fact_class: dict[str, Any] = {}
    for fact_class, verdict in sorted(verdict_by_class.items()):
        class_probes = [p for p in all_probes if p.fact_class == fact_class]
        per_fact_class[fact_class] = {
            "verdict": verdict,
            "fair_probe": _verdict_to_bool(verdict),
            "randomized_probes": sum(1 for p in class_probes if p.probe_kind == "randomized"),
            "paired_baseline_probes": sum(
                1 for p in class_probes if p.probe_kind == "paired_baseline"
            ),
            "causal_probes": sum(1 for p in class_probes if p.causal_verdict == CAUSAL),
        }

    document: dict[str, Any] = {
        "schema": FAIR_PROBE_RESULT_SCHEMA,
        "per_fact_class": per_fact_class,
        "probes": [p.to_dict() for p in all_probes],
    }
    sidecar_path = (
        _write_sidecar(output_dir, task_label, document) if output_dir else None
    )
    document["sidecar_path"] = sidecar_path
    return document


def fair_probe_bool_by_fact_class(join: dict[str, Any]) -> dict[str, bool | None]:
    """The ``fact_class -> fair_probe`` gate map the grader threads into ``ss_gate_readiness``:
    True for CAUSAL/CAUSAL_PAIRED, False for SELF_LOCALIZED, None (absent) for UNMEASURED."""
    out: dict[str, bool | None] = {}
    for fact_class, info in (join.get("per_fact_class") or {}).items():
        if isinstance(info, dict):
            out[fact_class] = info.get("fair_probe")
    return out


__all__ = [
    "CAUSAL_PAIRED",
    "FAIR_PROBE_RESULT_SCHEMA",
    "ControlResult",
    "ProbeResult",
    "compute_matched_probes",
    "fair_probe_bool_by_fact_class",
    "join_fair_probes",
]
