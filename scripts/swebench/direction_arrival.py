#!/usr/bin/env python3
"""DIRECTION-AND-ARRIVAL — GT's contribution is a DIRECTION; the trajectory answers
"did the agent arrive, and how fast".

GT is deterministic. Given the same repository state and the same agent observation it
emits the same evidence, so its per-task contribution is not a probability mass over
tokens — it is a SET of concrete directions: *this file*, *this symbol's contract*,
*this clause*, *pivot away from this loop*. Nothing about that set requires a model,
logprobs, or an attribution estimator. The only remaining empirical question is
behavioural and readable straight off the recorded trajectory:

    for each direction d that GT delivered, at which step did the agent's own state
    first SATISFY d — and was GT there first (AHEAD), behind the agent (BEHIND), or
    was the direction never taken at all (NEVER)?

and, paired against a GT-OFF trajectory for the SAME task, how many steps earlier did
the agent arrive at the SAME target when GT was pointing at it?

REUSE, NOT REINVENT. This module writes NO new parser and runs NO new GT replay. Every
primitive already exists and is already trusted by the grader:

* **directions** are read from the RECORDED artifacts of a GT-on run — the sealed
  runtime-ledger ``outcome=='delivered'`` rows — with the row's registered identity
  resolved by :func:`chronology_extract._row_evidence_type` /
  :func:`chronology_extract._row_fact_class`, and the delivered PAYLOAD BYTES taken from
  :func:`consumption_ledger.physical_delivery_authority` (the same seal join SS-LIVE
  Gate 1 uses). A row whose bytes never physically bound has NO delivery step and is
  skipped with a named reason — never guessed from ``iteration``.
* **delivery_step / arrival_step** live in ONE step space: the assistant-step counter of
  :func:`gt_performance_metrics._parse_timeline` (``step`` increments on every assistant
  message). A delivery's message index is mapped into that space by the timeline itself.
* **arrival for a file target** is the grader's own view/edit detection —
  ``_parse_timeline``'s ``viewed_file`` / ``edited_file`` compared with
  :func:`gt_performance_metrics._path_match`. There is no second path matcher here.
* **arrival for a symbol / contract / clause target** is the SAME passive-naming test
  :func:`chronology_extract._native_acquisition_index` uses for its BEHIND half:
  :func:`consumption_ledger._emitted_commands` for the model-authored commands and
  :func:`consumption_ledger._entity_patterns` / ``_named_in`` for word-boundary entity
  matching. ``_native_acquisition_index`` answers "did the agent get there BEFORE the
  delivery"; this module generalises the same scan to "when did the agent get there at
  all", on a GT-on OR a GT-off trajectory.

FAIL-CLOSED EVERYWHERE. No delivered rows -> an empty direction set plus a named reason.
A row that resolves to no registered fact class, or whose class has no declared target
shape, or that yields no extractable target, or whose bytes never bound -> SKIPPED with a
named reason and counted; never guessed. An arrival that never happens is ``None``
(censored at the trajectory end), never a fabricated budget. A paired delta whose either
side is censored is ``None`` with a named reason, never a fabricated maximum.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
from dataclasses import asdict, dataclass
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:  # allow `python scripts/swebench/direction_arrival.py`
    sys.path.insert(0, _HERE)

# REUSE the grader's ONE entity/command model (never a second divergent parser).
from consumption_ledger import (  # noqa: E402
    PHYSICAL_DELIVERY_BOUND,
    _block_entities,
    _emitted_commands,
    _entity_patterns,
    _named_in,
    build_consumption_ledger,
    physical_delivery_authority,
)

# REUSE the ledger-row identity resolution already trusted by the timing join.
from chronology_extract import _row_evidence_type, _row_fact_class  # noqa: E402

# REUSE the SOLE PRODUCER of an obligation clause's ``subject_symbols``. This is the exact
# function GT ran to decide, at delivery time, which symbols a deterministic exercise check
# may be RIGHT about (``obligations._credit_eligible_symbols`` reads its output). Running it
# here on the clause's own delivered verbatim reproduces GT's own structured answer instead
# of inventing a second, divergent text heuristic.
from groundtruth.pretask.spec import _v2_subject_symbols  # noqa: E402

DIRECTION_ARRIVAL_SCHEMA = "gt.direction_arrival.v1"

# --------------------------------------------------------------------------- #
# verdict + censoring vocabulary (closed sets — a reader may switch on these)
# --------------------------------------------------------------------------- #
AHEAD = "AHEAD"
BEHIND = "BEHIND"
NEVER = "NEVER"

#: the direction target SHAPES. Each is a different question about the agent's state.
KIND_FILE = "file"            # "edit/inspect THIS file"      -> arrival = viewed/edited it
KIND_CONTRACT = "contract"    # "preserve THIS symbol's contract" -> arrival = acted on symbol
KIND_CLAUSE = "clause"        # "satisfy THIS clause"         -> arrival = acted on its subject
KIND_PIVOT = "pivot"          # "stop looping / change course" -> arrival = next action at all

#: fact class -> the target shape GT's direction takes for that class. Derived by READING
#: §6.1 (what each of the 11 FACT classes actually delivers), not by pattern-guessing:
#:
#:   localization / def_partition / newfile_precedent / cochange_prior
#:       name a FILE to go to (ranked edit target, ownership boundary, sibling precedent,
#:       historically co-changing file). Arrival = the agent opened or edited that file.
#:   covering_red / syntax_result / submit_refusal
#:       name the FILE whose edit is unverified/broken/unsafe-to-submit. Arrival = the
#:       agent went back to that file. (These are verification directions; the target is
#:       the surface the agent must return to, which is exactly the row's file_path.)
#:   caller_contract / signature_delta
#:       name a SYMBOL whose contract must be preserved. A file-level arrival would be
#:       too coarse — the direction is satisfied only when the agent acts on the symbol.
#:   obligations
#:       names a CLAUSE the fix must satisfy; its target is the clause's STRUCTURED subject
#:       symbols — the same ``subject_symbols`` GT itself computed at delivery time (see
#:       ``_clause_targets``). A clause GT declared subject-less stays an honest named skip.
#:   recovery
#:       names no entity at all — it is "get off this loop". Arrival = the next agent
#:       action after the steer (a course change), which is only defined relative to a
#:       delivery, so it is deliberately NOT baseline-scannable.
_CLASS_TARGET_KIND: dict[str, str] = {
    "localization": KIND_FILE,
    "def_partition": KIND_FILE,
    "newfile_precedent": KIND_FILE,
    "cochange_prior": KIND_FILE,
    "covering_red": KIND_FILE,
    "syntax_result": KIND_FILE,
    "submit_refusal": KIND_FILE,
    "caller_contract": KIND_CONTRACT,
    "signature_delta": KIND_CONTRACT,
    "obligations": KIND_CLAUSE,
    "recovery": KIND_PIVOT,
}

# named SKIP reasons (a direction that could not be formed) — always counted, never guessed
SKIP_UNREGISTERED_FACT_CLASS = "unregistered_fact_class"
SKIP_UNMAPPED_TARGET_SHAPE = "unmapped_target_shape"
SKIP_NO_TARGET_EXTRACTABLE = "no_target_extractable"
#: CLAUSE sub-cases — an obligations payload is PROSE, so "no symbol in the bytes" splits
#: into two materially different facts and must never collapse into one blind reason:
#:   * the payload is not a recognised obligations render at all (a reader/writer drift
#:     signal — SOMETHING changed in the producer and the instrument must be told), and
#:   * the payload IS a recognised clause list, but not one clause carries a code
#:     identifier. That is not an instrument defect: it is GT's own verdict. The renderer
#:     stamps ``[could not be verified automatically]`` on exactly the clauses for which
#:     ``obligation_truth_statuses`` found ``not subjects`` — zero credit-eligible subject
#:     symbols. An arrival predicate for such a clause cannot exist without inventing one.
SKIP_CLAUSE_ROWS_UNPARSED = "clause_rows_unparsed"
SKIP_CLAUSE_SUBJECT_UNVERIFIABLE = "clause_subject_unverifiable"
SKIP_DELIVERY_UNBOUND = "delivery_bytes_never_bound"
SKIP_DELIVERY_STEP_UNRESOLVED = "delivery_step_unresolved"

# named CENSOR reasons (a direction formed, but a number could not honestly be produced)
CENSOR_GTON_NEVER_ARRIVED = "gton_never_arrived"
CENSOR_BASELINE_NEVER_ARRIVED = "baseline_never_arrived"
CENSOR_NO_BASELINE_TRAJECTORY = "no_baseline_trajectory"
CENSOR_PIVOT_NOT_BASELINE_SCANNABLE = "pivot_not_baseline_scannable"

# named EMPTY-SET reasons
EMPTY_NO_LEDGER = "no_runtime_ledger"
EMPTY_NO_DELIVERED_ROWS = "no_delivered_rows"
EMPTY_ALL_ROWS_SKIPPED = "all_delivered_rows_skipped"


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Direction:
    """One concrete target GT pointed the agent at, and the step it pointed."""

    direction_id: str
    fact_class: str
    kind: str
    target: str
    delivery_step: int | None
    delivery_msg_index: int | None
    ledger_row_index: int


@dataclass(frozen=True)
class Skip:
    """A delivered ledger row that could NOT become a direction, and exactly why."""

    ledger_row_index: int
    reason: str
    fact_class: str | None
    evidence_type: str | None


# --------------------------------------------------------------------------- #
# trajectory plumbing (ONE step space, shared by both arms)
# --------------------------------------------------------------------------- #
def _messages(trajectory: Any) -> list[dict]:
    if isinstance(trajectory, dict):
        msgs = trajectory.get("messages")
        return [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []
    if isinstance(trajectory, list):
        return [m for m in trajectory if isinstance(m, dict)]
    return []


def _timeline(messages: list[dict]) -> list[dict]:
    """The grader's ONE timeline (``gt_performance_metrics._parse_timeline``). Imported
    lazily so this module stays importable without the heavy metrics deps loaded up front."""
    try:
        from gt_performance_metrics import _parse_timeline
    except Exception:  # pragma: no cover - grader always has scripts/swebench on path
        return []
    return _parse_timeline(messages)


def _msg_index_to_step(timeline: list[dict]) -> dict[int, int]:
    """``message index -> assistant-step number`` in ``_parse_timeline``'s own step space.
    Observations carry the step of the assistant action that produced them, so a delivery
    rendered into an observation maps to the step it answers."""
    out: dict[int, int] = {}
    for entry in timeline:
        idx, step = entry.get("msg_index"), entry.get("step")
        if isinstance(idx, int) and isinstance(step, int):
            out.setdefault(idx, step)
    return out


# --------------------------------------------------------------------------- #
# DIRECTION LEDGER
# --------------------------------------------------------------------------- #
def _delivered_row_indices(rows: list[dict]) -> list[int]:
    out: list[int] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if row.get("outcome") != "delivered":
            continue
        try:
            chars = int(row.get("chars_delivered") or 0)
        except (TypeError, ValueError):
            continue
        if chars > 0:
            out.append(i)
    return out


def _physical_payloads(
    trajectory: Any, rows: list[dict]
) -> dict[int, tuple[int, str]]:
    """``ledger row index -> (delivery message index, exact delivered bytes)`` for every
    row whose sealed bytes PHYSICALLY bound to a model-visible span. Reuses the consumption
    ledger's physical delivery authority verbatim — a row that did not bind is simply
    absent (fail-closed), never approximated from ``iteration``."""
    out: dict[int, tuple[int, str]] = {}
    if trajectory is None:
        return out
    consumption = build_consumption_ledger(trajectory, runtime_ledger_rows=rows)
    deliveries = physical_delivery_authority(consumption).get("deliveries", {})
    for key, record in (deliveries or {}).items():
        if not isinstance(record, dict) or record.get("state") != PHYSICAL_DELIVERY_BOUND:
            continue
        try:
            row_index = int(key)
        except (TypeError, ValueError):
            continue
        msg_index = record.get("msg_index")
        if not isinstance(msg_index, int) or isinstance(msg_index, bool):
            continue
        out[row_index] = (msg_index, str(record.get("rendered_text") or ""))
    return out


# --------------------------------------------------------------------------- #
# CLAUSE target extraction (obligations) — structured, never a prose heuristic
# --------------------------------------------------------------------------- #
#: Every obligations renderer in ``groundtruth.runtime.obligations`` emits one clause per
#: line in the SAME shape: a bracketed status MARK followed by the clause verbatim in double
#: quotes (``render_obligation_truth_block`` / ``render_unexercised_block`` /
#: ``render_obligation_status_block``). Line-anchored so surrounding control prose, headers
#: and footers can never be mistaken for a clause.
_CLAUSE_ROW_RE = re.compile(r'^[^\S\n]*\[([^\]]*)\]\s*"([^"]*)"', re.MULTILINE)
#: ``obligation.resurface`` renders the same issue obligations as native bullets instead
#: (``gt_mini_patch._obligation_resurface_candidate``: ``"  - %s" % verbatim``). Same reader
#: shape ``obligations.rendered_obligation_subject_groups`` falls back to.
_CLAUSE_BULLET_RE = re.compile(r"^\s*-\s+(.+?)\s*$", re.MULTILINE)
#: ``render_unexercised_block`` prints the clause's OWN credit-eligible symbols INTO the
#: mark: ``no test/run output has mentioned `a`/`b```. When present these are GT's stored
#: ``subject_symbols`` verbatim — the strongest possible source, no re-derivation needed.
_CLAUSE_MARK_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")
#: the two truncation markers the renderers append (``_truncate_at_word`` -> "…";
#: ``render_obligation_status_block`` -> a raw ``verbatim[:157] + "..."``).
_CLAUSE_ELLIPSIS = ("…", "...")
#: ``obligations._credit_eligible_symbols``' own floor.
_CLAUSE_MIN_SYMBOL_LEN = 3


def _clause_quote_body(quote: str) -> str:
    """The part of a rendered clause quote whose tokens are certainly WHOLE.

    A rendered quote may be truncated. ``_truncate_at_word`` cuts at a word boundary in the
    common case but falls back to a hard cut on a single long token, and
    ``render_obligation_status_block`` hard-cuts unconditionally — so the token adjacent to
    an ellipsis marker may be a FRAGMENT. A fragment cannot fabricate an arrival (it would
    simply never match a command), but it can fabricate a *near* one, so it is dropped:
    strip the marker and discard the trailing token. Cost is at most one symbol, i.e. an
    honest skip. Never the reverse.
    """
    q = (quote or "").strip()
    for marker in _CLAUSE_ELLIPSIS:
        if q.endswith(marker):
            q = q[: -len(marker)].rstrip()
            return q.rsplit(None, 1)[0] if len(q.split()) > 1 else ""
    return q


def _clause_rows(payload: str) -> list[tuple[str, str]]:
    """``[(status mark, clause verbatim)]`` for every clause the payload renders."""
    rows = [(m.group(1), m.group(2)) for m in _CLAUSE_ROW_RE.finditer(payload or "")]
    if rows:
        return rows
    return [("", m.group(1)) for m in _CLAUSE_BULLET_RE.finditer(payload or "")]


def _clause_targets(payload: str) -> tuple[list[str], str | None]:
    """The subject symbols an obligations clause direction points at, + a named reason
    when none can be formed.

    STRUCTURED, in the product's own two forms, in preference order:

    1. symbols GT already printed into the status mark (its stored ``subject_symbols``);
    2. otherwise ``pretask.spec._v2_subject_symbols`` on the clause's own verbatim — the
       SOLE producer of those symbols, so this reproduces GT's answer rather than guessing.
       It admits only code-shaped names (backticked identifiers, call shapes, dotted names,
       snake_case, internal-capital, ``*Error``/``*Exception``/``*Warning``); ordinary
       English is rejected by construction. "cfn-lint passes" -> ``set()``.

    A content-word-overlap heuristic was REJECTED: on that same clause it yields
    ``{cfn-lint, passes}``, and "passes" matches nearly any command — a manufactured
    arrival, which is the one error this instrument may not make.
    """
    rows = _clause_rows(payload)
    if not rows:
        return [], SKIP_CLAUSE_ROWS_UNPARSED
    targets: set[str] = set()
    for mark, quote in rows:
        symbols = set(_CLAUSE_MARK_SYMBOL_RE.findall(mark or ""))
        if not symbols:
            symbols = set(_v2_subject_symbols(_clause_quote_body(quote)))
        targets |= {s for s in symbols if len(s) >= _CLAUSE_MIN_SYMBOL_LEN}
    if not targets:
        return [], SKIP_CLAUSE_SUBJECT_UNVERIFIABLE
    return sorted(targets), None


def _targets_for(kind: str, payload: str, file_path: str) -> tuple[list[str], str | None]:
    """The concrete target token(s) a direction of this shape names, plus the named reason
    when the direction cannot be formed (``None`` when it can).

    FILE shape -> the row's own ``file_path`` (the authoritative delivery subject).
    CONTRACT shape -> the SYMBOLS in the delivered bytes, extracted with
    ``consumption_ledger._block_entities`` (the grader's files+symbols extractor); when
    the bytes name no symbol the direction cannot be formed at symbol granularity and the
    caller records ``no_target_extractable`` rather than silently degrading to the file.
    CLAUSE shape -> the clause's structured SUBJECT symbols (see ``_clause_targets``);
    ``_block_entities`` is deliberately NOT used here — it reads structured evidence/contract
    markers that an obligations block never contains, so it skipped the class wholesale.
    PIVOT shape -> a single sentinel; the target is the course change, not an entity.
    """
    if kind == KIND_PIVOT:
        return ["<pivot>"], None
    if kind == KIND_FILE:
        return ([file_path], None) if file_path else ([], SKIP_NO_TARGET_EXTRACTABLE)
    if kind == KIND_CLAUSE:
        return _clause_targets(payload)
    _files, symbols = _block_entities(payload) if payload else (set(), set())
    return (sorted(symbols), None) if symbols else ([], SKIP_NO_TARGET_EXTRACTABLE)


def _build_directions(
    rows: list[dict], trajectory: Any = None
) -> tuple[list[Direction], list[Skip]]:
    """The DIRECTION LEDGER + the named skips, both deterministic."""
    payloads = _physical_payloads(trajectory, rows)
    steps = _msg_index_to_step(_timeline(_messages(trajectory))) if trajectory is not None else {}

    directions: list[Direction] = []
    skips: list[Skip] = []
    for row_index in _delivered_row_indices(rows):
        row = rows[row_index]
        evidence_type = _row_evidence_type(row)
        fact_class = _row_fact_class(evidence_type, row)
        if fact_class is None:
            skips.append(Skip(row_index, SKIP_UNREGISTERED_FACT_CLASS, None, evidence_type))
            continue
        kind = _CLASS_TARGET_KIND.get(fact_class)
        if kind is None:
            skips.append(
                Skip(row_index, SKIP_UNMAPPED_TARGET_SHAPE, fact_class, evidence_type)
            )
            continue

        bound = payloads.get(row_index)
        if trajectory is not None and bound is None:
            skips.append(Skip(row_index, SKIP_DELIVERY_UNBOUND, fact_class, evidence_type))
            continue
        msg_index, payload = bound if bound is not None else (None, "")
        delivery_step = steps.get(msg_index) if msg_index is not None else None
        if trajectory is not None and delivery_step is None:
            skips.append(
                Skip(row_index, SKIP_DELIVERY_STEP_UNRESOLVED, fact_class, evidence_type)
            )
            continue

        targets, no_target_reason = _targets_for(
            kind, payload, str(row.get("file_path") or "")
        )
        if not targets:
            skips.append(
                Skip(
                    row_index,
                    no_target_reason or SKIP_NO_TARGET_EXTRACTABLE,
                    fact_class,
                    evidence_type,
                )
            )
            continue
        for target in targets:
            directions.append(
                Direction(
                    direction_id=f"r{row_index}:{kind}:{target}",
                    fact_class=fact_class,
                    kind=kind,
                    target=target,
                    delivery_step=delivery_step,
                    delivery_msg_index=msg_index,
                    ledger_row_index=row_index,
                )
            )
    directions.sort(key=lambda d: (d.ledger_row_index, d.kind, d.target))
    skips.sort(key=lambda s: (s.ledger_row_index, s.reason))
    return directions, skips


def directions_from_ledger(rows: list[dict], *, trajectory: Any = None) -> list[Direction]:
    """The set D of concrete targets GT delivered on this task.

    ``rows`` are the raw runtime-ledger rows. ``trajectory`` is OPTIONAL: supply it (the
    normal case) and every direction's ``delivery_step`` is the AUTHORITATIVE physically
    bound delivery, and CONTRACT/CLAUSE targets are read from the exact delivered bytes.
    Omit it and only the ledger's own ``file_path`` subjects can be recovered, with
    ``delivery_step = None`` — honest, not guessed from ``iteration``."""
    return _build_directions(rows, trajectory)[0]


# --------------------------------------------------------------------------- #
# ARRIVAL SCAN
# --------------------------------------------------------------------------- #
def arrival_scan(trajectory: Any, directions: list[Direction]) -> dict[str, dict[str, Any]]:
    """For each direction, the FIRST step at which the agent's own state satisfies it.

    Pure trajectory-side: it never consults the delivery, so the identical scan runs over a
    GT-OFF trajectory for the same targets (the paired baseline). Returns
    ``{direction_id: {"arrival_step": int|None, "arrival_msg_index": int|None,
    "arrival_evidence": str|None, "skip_reason": str|None}}``.
    """
    messages = _messages(trajectory)
    timeline = _timeline(messages)
    assistant = [e for e in timeline if e.get("role") == "assistant"]

    out: dict[str, dict[str, Any]] = {}
    for direction in directions:
        out[direction.direction_id] = _scan_one(direction, assistant, messages)
    return out


def _miss(skip_reason: str | None = None) -> dict[str, Any]:
    return {
        "arrival_step": None,
        "arrival_msg_index": None,
        "arrival_evidence": None,
        "skip_reason": skip_reason,
    }


def _hit(entry: dict, evidence: str) -> dict[str, Any]:
    return {
        "arrival_step": int(entry.get("step")),
        "arrival_msg_index": entry.get("msg_index"),
        "arrival_evidence": evidence,
        "skip_reason": None,
    }


def _scan_one(
    direction: Direction, assistant: list[dict], messages: list[dict]
) -> dict[str, Any]:
    if direction.kind == KIND_PIVOT:
        # A pivot is a course change RELATIVE to the steer; with no delivery step there is
        # no "after", so it is not baseline-scannable. Fail closed with a named reason.
        if direction.delivery_step is None:
            return _miss(CENSOR_PIVOT_NOT_BASELINE_SCANNABLE)
        for entry in assistant:
            step = entry.get("step")
            idx = entry.get("msg_index")
            if not isinstance(step, int) or step <= direction.delivery_step:
                continue
            if isinstance(idx, int) and _emitted_commands(messages[idx]):
                return _hit(entry, "pivot_action")
        return _miss()

    if direction.kind == KIND_FILE:
        from gt_performance_metrics import _norm_path, _path_match

        target = _norm_path(direction.target)
        if not target:
            return _miss()
        for entry in assistant:
            edited = _norm_path(str(entry.get("edited_file") or ""))
            viewed = _norm_path(str(entry.get("viewed_file") or ""))
            if edited and _path_match(edited, target):
                return _hit(entry, "edited")
            if viewed and _path_match(viewed, target):
                return _hit(entry, "viewed")
        return _miss()

    # CONTRACT / CLAUSE: the SAME passive-naming test _native_acquisition_index uses —
    # a model-authored command naming the target entity on a word boundary.
    pats = _entity_patterns(set(), {direction.target})
    if not pats:
        return _miss()
    for entry in assistant:
        idx = entry.get("msg_index")
        if not isinstance(idx, int):
            continue
        for cmd in _emitted_commands(messages[idx]):
            if _named_in(cmd, pats):
                return _hit(entry, "named_in_command")
    return _miss()


# --------------------------------------------------------------------------- #
# VERDICTS
# --------------------------------------------------------------------------- #
def _verdict(delivery_step: int | None, arrival_step: int | None) -> str:
    """AHEAD when GT pointed BEFORE the agent got there; BEHIND when the agent was already
    there at-or-before the delivery; NEVER when the agent never arrived (censored at the
    trajectory end). A direction with no delivery step cannot be AHEAD of anything, so an
    arrival with an unknown delivery is BEHIND (conservative: never credits GT)."""
    if arrival_step is None:
        return NEVER
    if delivery_step is None:
        return BEHIND
    return AHEAD if delivery_step < arrival_step else BEHIND


# --------------------------------------------------------------------------- #
# artifact discovery (fail-closed)
# --------------------------------------------------------------------------- #
def _find_one(task_dir: str, pattern: str) -> str | None:
    hits = sorted(glob.glob(os.path.join(task_dir, pattern)))
    if not hits:
        hits = sorted(glob.glob(os.path.join(task_dir, "**", pattern), recursive=True))
    return hits[0] if hits else None


def _load_ledger_rows(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                rows.append(row if isinstance(row, dict) else {})
    except OSError:
        return []
    return rows


def _load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# ADJUDICATION
# --------------------------------------------------------------------------- #
def adjudicate(
    gton_task_dir: str, gtoff_traj_path: str | None = None
) -> dict[str, Any]:
    """The full DIRECTION-AND-ARRIVAL instrument for one task.

    Reads the GT-on task directory's RECORDED artifacts (``gt_runtime_ledger*.jsonl`` +
    ``mini-swe-agent.trajectory.json``), builds the direction ledger, scans arrival on the
    GT-on trajectory and — when ``gtoff_traj_path`` is given — on the frozen GT-OFF
    trajectory for the SAME task at the SAME targets, then emits per-direction rows and a
    summary. Every unavailable number is ``None`` with a named reason."""
    ledger_path = _find_one(gton_task_dir, "gt_runtime_ledger*.jsonl")
    traj_path = _find_one(gton_task_dir, "mini-swe-agent.trajectory.json")

    reasons: list[str] = []
    rows = _load_ledger_rows(ledger_path) if ledger_path else []
    if not ledger_path:
        reasons.append(EMPTY_NO_LEDGER)
    trajectory = _load_json(traj_path) if traj_path else None

    delivered_indices = _delivered_row_indices(rows)
    if ledger_path and not delivered_indices:
        reasons.append(EMPTY_NO_DELIVERED_ROWS)

    directions, skips = _build_directions(rows, trajectory)
    if delivered_indices and not directions:
        reasons.append(EMPTY_ALL_ROWS_SKIPPED)

    gton = arrival_scan(trajectory, directions) if trajectory is not None else {}

    baseline_traj = _load_json(gtoff_traj_path) if gtoff_traj_path else None
    baseline = arrival_scan(baseline_traj, directions) if baseline_traj is not None else {}

    per_direction: list[dict[str, Any]] = []
    deltas: list[int] = []
    censored: dict[str, int] = {}
    verdict_counts = {AHEAD: 0, BEHIND: 0, NEVER: 0}

    for direction in directions:
        on = gton.get(direction.direction_id) or _miss()
        off = baseline.get(direction.direction_id) or _miss(
            CENSOR_NO_BASELINE_TRAJECTORY if baseline_traj is None else None
        )
        verdict = _verdict(direction.delivery_step, on["arrival_step"])
        verdict_counts[verdict] += 1

        delta: int | None = None
        censor_reason: str | None = None
        if baseline_traj is None:
            censor_reason = CENSOR_NO_BASELINE_TRAJECTORY
        elif off.get("skip_reason"):
            censor_reason = str(off["skip_reason"])
        elif on["arrival_step"] is None:
            censor_reason = CENSOR_GTON_NEVER_ARRIVED
        elif off["arrival_step"] is None:
            censor_reason = CENSOR_BASELINE_NEVER_ARRIVED
        else:
            delta = int(off["arrival_step"]) - int(on["arrival_step"])
            deltas.append(delta)
        if censor_reason is not None:
            censored[censor_reason] = censored.get(censor_reason, 0) + 1

        per_direction.append({
            **asdict(direction),
            "verdict": verdict,
            "arrival_step": on["arrival_step"],
            "arrival_msg_index": on["arrival_msg_index"],
            "arrival_evidence": on["arrival_evidence"],
            "arrival_skip_reason": on["skip_reason"],
            "baseline_arrival_step": off["arrival_step"],
            "baseline_arrival_evidence": off["arrival_evidence"],
            "arrival_delta": delta,
            "arrival_delta_censor_reason": censor_reason,
        })

    skip_counts: dict[str, int] = {}
    for skip in skips:
        skip_counts[skip.reason] = skip_counts.get(skip.reason, 0) + 1

    return {
        "schema": DIRECTION_ARRIVAL_SCHEMA,
        "gton_task_dir": gton_task_dir,
        "gton_trajectory_path": traj_path,
        "runtime_ledger_path": ledger_path,
        "gtoff_trajectory_path": gtoff_traj_path,
        "per_direction": per_direction,
        "skipped_rows": [asdict(s) for s in skips],
        "summary": {
            "delivered_rows": len(delivered_indices),
            "directions": len(directions),
            "empty_reasons": sorted(set(reasons)),
            "verdicts": dict(verdict_counts),
            "skipped_rows_total": len(skips),
            "skipped_reasons": dict(sorted(skip_counts.items())),
            "n_measured_pairs": len(deltas),
            "median_arrival_delta": (
                float(statistics.median(deltas)) if deltas else None
            ),
            "n_censored": sum(censored.values()),
            "censor_reasons": dict(sorted(censored.items())),
        },
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _render(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"schema: {report['schema']}",
        f"task dir: {report['gton_task_dir']}",
        f"ledger: {report['runtime_ledger_path']}",
        f"trajectory: {report['gton_trajectory_path']}",
        f"baseline: {report['gtoff_trajectory_path']}",
        "",
        f"delivered rows: {s['delivered_rows']}   directions: {s['directions']}"
        f"   skipped rows: {s['skipped_rows_total']} {s['skipped_reasons'] or ''}",
        f"verdicts: AHEAD={s['verdicts'][AHEAD]}  BEHIND={s['verdicts'][BEHIND]}"
        f"  NEVER={s['verdicts'][NEVER]}",
        f"median arrival_delta: {s['median_arrival_delta']}"
        f"  (measured pairs {s['n_measured_pairs']}, censored {s['n_censored']}"
        f" {s['censor_reasons'] or ''})",
    ]
    if s["empty_reasons"]:
        lines.append(f"empty reasons: {s['empty_reasons']}")
    lines.append("")
    lines.append(
        f"{'fact_class':<20} {'kind':<9} {'deliv':>5} {'arriv':>5} {'base':>5} "
        f"{'delta':>6}  verdict   target"
    )
    for row in report["per_direction"]:
        lines.append(
            f"{row['fact_class']:<20} {row['kind']:<9} "
            f"{str(row['delivery_step']):>5} {str(row['arrival_step']):>5} "
            f"{str(row['baseline_arrival_step']):>5} {str(row['arrival_delta']):>6}  "
            f"{row['verdict']:<9} {row['target']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "DIRECTION-AND-ARRIVAL: did the agent arrive at the targets GT pointed at, "
            "and how fast?"
        )
    )
    parser.add_argument("gton_task_dir", help="a GT-on run's per-task artifact directory")
    parser.add_argument(
        "--gtoff-traj", default=None,
        help="frozen GT-OFF mini-swe-agent.trajectory.json for the SAME task",
    )
    parser.add_argument("--json", action="store_true", help="emit the raw JSON report")
    args = parser.parse_args(argv)

    report = adjudicate(args.gton_task_dir, args.gtoff_traj)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_render(report))
    return 0


__all__ = [
    "AHEAD",
    "BEHIND",
    "DIRECTION_ARRIVAL_SCHEMA",
    "Direction",
    "KIND_CLAUSE",
    "KIND_CONTRACT",
    "KIND_FILE",
    "KIND_PIVOT",
    "NEVER",
    "Skip",
    "adjudicate",
    "arrival_scan",
    "directions_from_ledger",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
