#!/usr/bin/env python3
"""SPEC-J3 — the timing JOIN: trajectory + delivered ledger rows -> the six exact
message indices of :class:`chronological_adjudication.Chronology`, adjudicated into
per-fact-class timing verdicts feeding the grader's ``correct_rl_adhered_time`` gate.

The pure timing authority (``groundtruth.runtime.chronological_adjudication.adjudicate``)
already exists and is fail-closed; NOTHING fed it. This module is that feed. It is
PURE / DETERMINISTIC / stdlib-only and **fail-closed by construction**: every index is an
EXACT message index or ``None`` — never a guess — and a missing join yields ``None`` so the
adjudicator returns ``UNMEASURED``.

REUSE, NOT REINVENT (the reader/writer-mismatch discipline). Every join reuses the
``consumption_ledger`` machinery already trusted by the grader:

* ``delivery_index`` — the ledger row's exact rendered bytes located in the trajectory via
  :func:`consumption_ledger._locate_seal_spans` (the SAME seal join
  ``consumption_ledger._build_v2`` uses for SS-LIVE Gate 1; ambiguous == unjoined, exactly
  as that path drops an ambiguous seal). When a row predates seals, the legacy
  ``iteration -> tool_ordinal`` mapping (the running count of ``role=='tool'`` messages,
  the mapping ``_build_v2`` uses for ``legacy_iteration``) is the documented fallback — but
  a seal-less row cannot be graded anyway (``adjudicate`` requires a 16-hex delivery seal),
  so it is ``UNMEASURED`` regardless.
* entity extraction for ``native_acquisition_index`` reuses
  :func:`consumption_ledger._block_entities` (the grader-side files+symbols extractor). The
  RUNTIME ack scanner's own extractor (``_ss_extract_paths`` / ``_ss_extract_symbols`` in
  ``artifact_deepswe.gt_mini_patch``) is NOT importable into a pure grader — importing the
  seam pulls its whole runtime with side effects — so the grader's pure equivalent is used.
* event classification for ``decision_open_index`` reuses
  ``gt_performance_metrics._parse_timeline`` (the grader's ONE source of truth for
  is_search / is_edit / is_test / viewed_file), imported lazily so this module stays cheap.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from groundtruth.runtime.chronological_adjudication import (
    LATE,
    ON_TIME,
    STEP_BEHIND,
    UNMEASURED,
    Chronology,
    adjudicate,
)
from groundtruth.runtime.fact_registry import EVENTS, registration_for, required_event

# REUSE the consumption-ledger join machinery (do NOT reinvent a second divergent join).
from consumption_ledger import (
    _action_kind,
    _block_entities,
    _emitted_commands,
    _entity_patterns,
    _locate_seal_spans,
    _named_in,
)

TIMING_JOIN_SCHEMA = "gt.chronological_timing_join.v1"

_SHA16_WIDTH = 16


def _valid_seal(value: object) -> bool:
    """True iff ``value`` is exactly 16 lowercase-hex chars (the adjudicator's precondition)."""
    return (
        isinstance(value, str)
        and len(value) == _SHA16_WIDTH
        and all(char in "0123456789abcdef" for char in value)
    )


@dataclass(frozen=True)
class ExtractedChronology:
    """One delivered row's exact chronology + its adjudicated timing, for audit."""

    ledger_row_index: int
    evidence_type: str
    fact_class: str | None
    delivery_seal: str
    actual_event: str
    chronology: Chronology
    timing_verdict: str
    unmeasured_reason: str | None


# --------------------------------------------------------------------------- #
# trajectory helpers (message-index space — the SAME space consumption_ledger joins in)
# --------------------------------------------------------------------------- #
def _messages(trajectory: Any) -> list[dict]:
    if isinstance(trajectory, dict):
        msgs = trajectory.get("messages")
        return [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []
    if isinstance(trajectory, list):
        return [m for m in trajectory if isinstance(m, dict)]
    return []


def _visible_buffers(messages: list[dict]) -> list[str]:
    """Per-message model-visible content, exactly as consumption_ledger._build_v2 builds it
    for the seal join (user/tool/system + function_call_output; everything else empty)."""
    buffers: list[str] = []
    for m in messages:
        role, mtype = m.get("role"), m.get("type")
        content = m.get("content")
        buffers.append(
            content
            if isinstance(content, str)
            and (
                role in ("user", "tool", "system")
                or mtype == "function_call_output"
            )
            else ""
        )
    return buffers


def _tool_ordinal_to_index(messages: list[dict]) -> dict[int, int]:
    """``iteration -> message index`` via the running count of ``role=='tool'`` messages —
    the exact ``tool_ordinal`` mapping consumption_ledger._build_v2 uses for its legacy
    ``iteration`` join. The Nth tool message carries iteration N."""
    out: dict[int, int] = {}
    ordinal = 0
    for i, m in enumerate(messages):
        if m.get("role") == "tool":
            ordinal += 1
            out.setdefault(ordinal, i)
    return out


def _boundary_indices(messages: list[dict]) -> dict[str, list[int]]:
    """Message indices of each fine observation boundary, keyed by the fact_registry EVENTS
    name. Reuses gt_performance_metrics._parse_timeline (one source of truth for command
    classification): each observation carries the RESULT of its preceding assistant action,
    so the boundary kind is read from that action's is_search / is_edit / is_test / view."""
    try:
        from gt_performance_metrics import _parse_timeline
    except Exception:  # pragma: no cover - grader always has scripts/swebench on path
        return {}
    timeline = _parse_timeline(messages)
    out: dict[str, list[int]] = {ev: [] for ev in EVENTS}
    # task_start = the first model-visible user message (the step-0 brief boundary).
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            out["task_start"].append(i)
            break
    prev: dict | None = None
    last_obs_index: int | None = None
    for entry in timeline:
        role = entry.get("role")
        idx = entry.get("msg_index")
        if not isinstance(idx, int):
            prev = entry if role == "assistant" else prev
            continue
        if role == "observation":
            last_obs_index = idx
            if prev is not None and prev.get("role") == "assistant":
                if prev.get("is_search"):
                    out["search_result"].append(idx)
                    # a search observation is also the failed_search/ADD boundary candidate.
                    out["failed_search"].append(idx)
                elif prev.get("is_edit"):
                    out["edit_result"].append(idx)
                    out["first_view_edit"].append(idx)
                elif prev.get("is_test"):
                    out["test_result"].append(idx)
                elif prev.get("viewed_file"):
                    out["file_view"].append(idx)
                    out["first_view_edit"].append(idx)
            if entry.get("has_build_fail"):
                out["failure_obs"].append(idx)
        else:
            prev = entry
    # submit = the last observation boundary (the completion/submit interception window),
    # or an explicit exit message when present.
    exit_index = next(
        (i for i, m in enumerate(messages) if m.get("role") == "exit"), None
    )
    submit_index = exit_index if exit_index is not None else last_obs_index
    if submit_index is not None:
        out["submit"].append(submit_index)
    for ev in out:
        out[ev] = sorted(set(out[ev]))
    return out


# --------------------------------------------------------------------------- #
# per-row evidence identity
# --------------------------------------------------------------------------- #
def _row_evidence_type(row: dict) -> str:
    """The finest shipped evidence_type the row carries (lineage ``evidence_type`` first,
    then a ``gateway.<type>`` layer, then the bare layer). Resolution to a registration is
    the adjudicator's job — an unresolved type simply fails closed to UNMEASURED."""
    et = row.get("evidence_type")
    if isinstance(et, str) and et.strip() and registration_for(et) is not None:
        return et.strip()
    layer = str(row.get("layer") or "").strip()
    if layer.startswith("gateway."):
        candidate = layer.split(".", 1)[1]
        if registration_for(candidate) is not None:
            return candidate
    if isinstance(et, str) and et.strip():
        return et.strip()
    return layer


def _row_fact_class(evidence_type: str, row: dict) -> str | None:
    """Canonical registered fact class for the row, or ``None`` when it resolves to none."""
    reg = registration_for(evidence_type)
    if reg is not None:
        return reg.fact_class
    fc = row.get("fact_class")
    return fc if isinstance(fc, str) and registration_for(fc) is not None else None


def _row_actual_event(row: dict) -> str:
    """The row's fine observation event — the seam's ``event_type`` (SPEC), falling back to
    the lineage ``actual_event`` when ``event_type`` is empty."""
    ev = row.get("event_type")
    if isinstance(ev, str) and ev.strip():
        return ev.strip()
    ev2 = row.get("actual_event")
    return ev2.strip() if isinstance(ev2, str) else ""


# --------------------------------------------------------------------------- #
# the six index derivations
# --------------------------------------------------------------------------- #
def _delivery_index_and_payload(
    row: dict,
    messages: list[dict],
    buffers: list[str],
    tool_ordinal_index: dict[int, int],
) -> tuple[int | None, str]:
    """The exact message index the row was delivered at + its rendered payload bytes.

    Primary = the seal join (consumption_ledger._locate_seal_spans): the exact sealed
    window(s) in the model-visible buffers. When several byte-identical windows exist the
    home is resolved EXACTLY as consumption_ledger._build_v2 does: the earliest unclaimed
    window at or after the row's authoritative delivery boundary (iteration -> tool_ordinal
    message index). Windows only before that boundary are a pre-delivery collision and the
    row is unjoined (fail-closed). Fallback = the legacy iteration -> tool_ordinal mapping
    for a seal-less row (which is UNMEASURED anyway — no seal to grade)."""
    seal = row.get("content_sha256_16")
    chars = int(row.get("chars_delivered") or 0)
    if _valid_seal(seal) and chars > 0:
        candidates: list[tuple[int, tuple[int, int]]] = []
        for msg_index, content in enumerate(buffers):
            for span in _locate_seal_spans(content, chars, str(seal)):
                candidates.append((msg_index, span))
        if candidates:
            it_row = row.get("iteration")
            boundary = (
                tool_ordinal_index.get(it_row)
                if isinstance(it_row, int) and not isinstance(it_row, bool)
                else None
            )
            eligible = (
                [c for c in candidates if c[0] >= boundary]
                if boundary is not None else candidates
            )
            if eligible:
                mi, (start, end) = eligible[0]
                return mi, buffers[mi][start:end]
            return None, ""  # every window precedes the delivery boundary — fail-closed
    # legacy fallback: iteration is the tool_ordinal (Nth tool message).
    it = row.get("iteration")
    if isinstance(it, int) and not isinstance(it, bool):
        mi = tool_ordinal_index.get(it)
        if mi is not None:
            return mi, ""
    return None, ""


def _decision_open_index(
    required: str | None, delivery_index: int | None, boundaries: dict[str, list[int]]
) -> int | None:
    """The message index where the row's target decision opened: the GREATEST boundary of the
    fact class's required event that is at-or-before delivery (the boundary the delivery is
    answering). ``None`` (fail-closed) when the boundary never opened before delivery."""
    if not required or delivery_index is None:
        return None
    opens = boundaries.get(required) or []
    prior = [b for b in opens if b <= delivery_index]
    return max(prior) if prior else None


def _decision_commit_index(
    messages: list[dict], decision_open_index: int | None
) -> int | None:
    """The message index of the agent's next STATE-CHANGING action after the boundary — the
    first assistant message strictly after ``decision_open_index`` emitting a repository-
    mutating command (consumption_ledger._action_kind == 'mutation'). Reads/searches/views do
    not change state. ``None`` (fail-closed) when the decision was never committed."""
    if decision_open_index is None:
        return None
    for j in range(decision_open_index + 1, len(messages)):
        if messages[j].get("role") != "assistant":
            continue
        for cmd in _emitted_commands(messages[j]):
            if _action_kind(cmd) == "mutation":
                return j
    return None


def _native_acquisition_index(
    messages: list[dict], delivery_index: int | None, payload: str, file_path: str
) -> int | None:
    """The earliest message where the agent ITSELF acquired the delivered fact BEFORE GT
    delivered it — a passive grep/read/view command (not a mutation/verification) naming a
    delivered entity, strictly before ``delivery_index``. Entities are extracted with
    consumption_ledger._block_entities (payload bytes preferred; else the row's file_path).
    ``None`` when the agent did not self-acquire the fact first."""
    if delivery_index is None:
        return None
    files, symbols = _block_entities(payload) if payload else (set(), set())
    if not files and not symbols and file_path:
        import os as _os

        files = {file_path, _os.path.basename(file_path)}
    pats = _entity_patterns(files, symbols)
    if not pats:
        return None
    for j in range(0, delivery_index):
        if messages[j].get("role") != "assistant":
            continue
        for cmd in _emitted_commands(messages[j]):
            # passive acquisition only: a mutation/verification is not a self-acquisition.
            if _action_kind(cmd) is None and _named_in(cmd, pats):
                return j
    return None


def _acknowledgment_index(
    seal: str,
    ledger_rows: list[dict],
    tool_ordinal_index: dict[int, int],
) -> int | None:
    """The message index of the ledger's own consumption ACK for this delivery — a row with
    ``event_type=='ack'`` and ``reason=='ss_ack'`` sealed to the SAME delivery bytes, mapped
    to a message index by its iteration (tool_ordinal). ``None`` when no ack row exists.
    (Feeds only the causal fair-probe branch; it never alters the timing verdict.)"""
    if not _valid_seal(seal):
        return None
    for row in ledger_rows:
        if (
            row.get("event_type") == "ack"
            and row.get("reason") == "ss_ack"
            and row.get("content_sha256_16") == seal
        ):
            it = row.get("iteration")
            if isinstance(it, int) and not isinstance(it, bool):
                return tool_ordinal_index.get(it)
    return None


def _action_index(
    messages: list[dict], delivery_index: int | None, payload: str, file_path: str
) -> int | None:
    """The message index of the agent's first relevant post-delivery action on the delivered
    entity — a mutation/verification command naming a delivered entity, strictly after
    ``delivery_index`` (the consumption-ledger 'acted' receipt). ``None`` otherwise. (Feeds
    only the causal fair-probe branch; it never alters the timing verdict.)"""
    if delivery_index is None:
        return None
    files, symbols = _block_entities(payload) if payload else (set(), set())
    if not files and not symbols and file_path:
        import os as _os

        files = {file_path, _os.path.basename(file_path)}
    pats = _entity_patterns(files, symbols)
    if not pats:
        return None
    for j in range(delivery_index + 1, len(messages)):
        if messages[j].get("role") != "assistant":
            continue
        for cmd in _emitted_commands(messages[j]):
            if _action_kind(cmd) in ("mutation", "verification") and _named_in(cmd, pats):
                return j
    return None


def _unmeasured_reason(
    *,
    seal: str,
    evidence_type: str,
    delivery_index: int | None,
    decision_open_index: int | None,
    decision_commit_index: int | None,
    actual_event: str,
    verdict: str,
) -> str | None:
    """The FIRST missing join that made a row UNMEASURED (observability only)."""
    if verdict != UNMEASURED:
        return None
    if not _valid_seal(seal):
        return "no_delivery_seal"
    if registration_for(evidence_type) is None or required_event(evidence_type) is None:
        return "unregistered_evidence_type"
    if delivery_index is None:
        return "delivery_unjoined"
    if decision_open_index is None:
        return "decision_open_unresolved"
    if decision_commit_index is None:
        return "decision_commit_unresolved"
    if actual_event not in EVENTS:
        return "event_not_in_vocabulary"
    return "adjudicator_unmeasured"


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def extract_chronologies(
    trajectory: Any, ledger_rows: list[dict]
) -> dict[int, ExtractedChronology]:
    """Build the six exact-index chronology for EVERY delivered ledger row.

    Keyed by the row's index in ``ledger_rows`` (stable, points at the exact row). A row is
    'delivered' when ``outcome=='delivered'`` and ``chars_delivered>0``. Every index is an
    exact message index or ``None``; a missing join is ``None`` so ``adjudicate`` returns
    ``UNMEASURED`` (fail-closed by construction)."""
    messages = _messages(trajectory)
    buffers = _visible_buffers(messages)
    tool_ordinal_index = _tool_ordinal_to_index(messages)
    boundaries = _boundary_indices(messages)

    out: dict[int, ExtractedChronology] = {}
    for row_index, row in enumerate(ledger_rows):
        if not isinstance(row, dict):
            continue
        if row.get("outcome") != "delivered" or int(row.get("chars_delivered") or 0) <= 0:
            continue

        evidence_type = _row_evidence_type(row)
        fact_class = _row_fact_class(evidence_type, row)
        actual_event = _row_actual_event(row)
        seal = row.get("content_sha256_16")
        seal_str = str(seal) if isinstance(seal, str) else ""
        file_path = str(row.get("file_path") or "")

        delivery_index, payload = _delivery_index_and_payload(
            row, messages, buffers, tool_ordinal_index
        )
        required = required_event(evidence_type)
        decision_open = _decision_open_index(required, delivery_index, boundaries)
        decision_commit = _decision_commit_index(messages, decision_open)
        native_acq = _native_acquisition_index(
            messages, delivery_index, payload, file_path
        )
        ack_index = _acknowledgment_index(seal_str, ledger_rows, tool_ordinal_index)
        act_index = _action_index(messages, delivery_index, payload, file_path)

        chronology = Chronology(
            decision_open_index=decision_open,
            delivery_index=delivery_index,
            decision_commit_index=decision_commit,
            native_acquisition_index=native_acq,
            acknowledgment_index=ack_index,
            action_index=act_index,
        )

        if _valid_seal(seal_str):
            verdict = adjudicate(
                evidence_type=evidence_type,
                actual_event=actual_event,
                delivery_seal=seal_str,
                chronology=chronology,
            ).timing_verdict
        else:
            verdict = UNMEASURED  # no seal to grade -> fail-closed

        reason = _unmeasured_reason(
            seal=seal_str,
            evidence_type=evidence_type,
            delivery_index=delivery_index,
            decision_open_index=decision_open,
            decision_commit_index=decision_commit,
            actual_event=actual_event,
            verdict=verdict,
        )
        out[row_index] = ExtractedChronology(
            ledger_row_index=row_index,
            evidence_type=evidence_type,
            fact_class=fact_class,
            delivery_seal=seal_str,
            actual_event=actual_event,
            chronology=chronology,
            timing_verdict=verdict,
            unmeasured_reason=reason,
        )
    return out


def _class_verdict(verdicts: list[str]) -> tuple[str, bool | None]:
    """Roll delivered-row verdicts up to one class verdict + its ``correct_time`` bit:
    ON_TIME only when every row is ON_TIME; LATE/STEP_BEHIND when any row is; else UNMEASURED.
    ``correct_time``: ON_TIME->True, LATE/STEP_BEHIND->False, UNMEASURED->None (fail-closed)."""
    if verdicts and all(v == ON_TIME for v in verdicts):
        return ON_TIME, True
    if any(v == LATE for v in verdicts):
        return LATE, False
    if any(v == STEP_BEHIND for v in verdicts):
        return STEP_BEHIND, False
    return UNMEASURED, None


def adjudicate_deliveries(trajectory: Any, ledger_rows: list[dict]) -> dict[str, Any]:
    """Adjudicate every delivered row and roll up per canonical fact class.

    Returns a JSON-safe diagnostic (``gt.chronological_timing_join.v1``): ``per_fact_class``
    carries each class's timing verdict, its ``correct_time`` bit for the
    ``correct_rl_adhered_time`` gate, the row counts, and any UNMEASURED reasons; ``deliveries``
    is the per-row audit trail. Rows that resolve to no registered class are recorded in
    ``deliveries`` but never override a real class (fail-closed)."""
    chronologies = extract_chronologies(trajectory, ledger_rows)

    by_class: dict[str, list[ExtractedChronology]] = {}
    deliveries: list[dict[str, Any]] = []
    for row_index in sorted(chronologies):
        ec = chronologies[row_index]
        deliveries.append(
            {
                "ledger_row_index": ec.ledger_row_index,
                "evidence_type": ec.evidence_type,
                "fact_class": ec.fact_class,
                "delivery_seal": ec.delivery_seal,
                "actual_event": ec.actual_event,
                "timing_verdict": ec.timing_verdict,
                "unmeasured_reason": ec.unmeasured_reason,
                "chronology": asdict(ec.chronology),
            }
        )
        if ec.fact_class is not None:
            by_class.setdefault(ec.fact_class, []).append(ec)

    per_fact_class: dict[str, Any] = {}
    for fact_class, rows in sorted(by_class.items()):
        verdicts = [r.timing_verdict for r in rows]
        verdict, correct_time = _class_verdict(verdicts)
        reasons = sorted(
            {r.unmeasured_reason for r in rows if r.unmeasured_reason is not None}
        )
        per_fact_class[fact_class] = {
            "verdict": verdict,
            "correct_time": correct_time,
            "rows_total": len(rows),
            "rows_on_time": sum(1 for v in verdicts if v == ON_TIME),
            "rows_late": sum(1 for v in verdicts if v == LATE),
            "rows_step_behind": sum(1 for v in verdicts if v == STEP_BEHIND),
            "rows_unmeasured": sum(1 for v in verdicts if v == UNMEASURED),
            "unmeasured_reasons": reasons,
        }

    return {
        "schema": TIMING_JOIN_SCHEMA,
        "delivered_rows_graded": len(chronologies),
        "per_fact_class": per_fact_class,
        "deliveries": deliveries,
    }


def timing_by_fact_class(join: dict[str, Any]) -> dict[str, bool | None]:
    """The ``fact_class -> correct_time`` map the grader threads into ``ss_gate_readiness``'s
    ``chronological_time`` parameter. Only classes with a measured verdict carry True/False;
    everything else is absent (the caller's ``.get`` yields ``None`` -> gate stays UNMEASURED)."""
    out: dict[str, bool | None] = {}
    for fact_class, info in (join.get("per_fact_class") or {}).items():
        if isinstance(info, dict):
            out[fact_class] = info.get("correct_time")
    return out


__all__ = [
    "TIMING_JOIN_SCHEMA",
    "ExtractedChronology",
    "adjudicate_deliveries",
    "extract_chronologies",
    "timing_by_fact_class",
]
