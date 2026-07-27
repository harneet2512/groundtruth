#!/usr/bin/env python3
"""Per-feature verdict reporter for the 17 DIRECT GroundTruth features.

Answers ONE question from a completed run's artifacts: for each of the 17 DIRECT
features (10 FACT + 7 CAP byte owners), is it working, and did it deliver at the
boundary its contract names?

THE THREE VERDICTS -- the distinction is the whole point:

  FIRED             the trigger occurred AND evidence reached the model
                    (``outcome == "delivered"`` AND ``chars_delivered > 0``).
  TRIGGER-ABSENT    the trigger never occurred in this trajectory.  CORRECT-QUIET.
                    A legitimate outcome, NEVER a feature failure.  Six of the 17
                    are MISTAKE-GATED: they need the agent to write a syntax error,
                    submit dirty, break a signature, create a new file, stall, or
                    leave a covering test failing.  A competent agent produces few
                    of those, so a dark mistake-gated feature is evidence the run
                    went well -- not evidence the feature broke.
  DELIVERY-FAILURE  the trigger DID occur and evidence was produced, but the bytes
                    never reached the model.  THIS IS THE ONLY REAL FAILURE.

Everything the tool asserts is derived from executable authorities, never re-typed:

  * the 17 features + each one's contracted boundary
        ``groundtruth.runtime.reasoning_runtime``:
        ``_FACT_DECISION_CONTRACTS`` (10 FACT), ``_CAP_FACT_BINDING`` (7 CAP),
        ``feature_contract_for(f).commitment_boundary``
  * the registry row (``deliver_by`` / ``surface``)
        ``groundtruth.runtime.fact_registry.REGISTRY``
  * producer layer -> fact class
        ``gt_mini_patch._LAYER_TO_FACT_CLASS`` / ``_fact_identity_for_layer``
  * CAP byte-owner attribution
        ``gt_mini_patch._LANE_PROFILE_MEMBER_OWNERS`` + the row's own
        ``profile_member`` / ``feature_ids`` lineage.

THREE VOCABULARY TRAP (read before touching the on-time logic).  Three different
fields each LOOK like "the boundary" and none of them is interchangeable:

  ``contracted_boundary``  EVENT vocabulary   (task_start, search_result, file_view,
                                               edit_result, test_result, submit,
                                               failed_search, failure_obs, ...)
  ``surface``              SURFACE vocabulary (brief, post_search, post_view,
                                               post_edit, post_test, submit, steer)
  ``event_type``           the ledger's own free-form channel string, which mixes
                           surface names (post_view/post_edit), event names
                           (test_result), producer names (l3.contract) and "".

Comparing the wrong pair manufactures a false "0 on-time".  So on-time is scored
ONLY from ``contracted_boundary`` against an observed value that is itself in
``fact_registry.EVENTS``.  Missing either side => NOT-EVALUABLE.  Never guessed.
(Run 30225435976 proves the trap is live: its lineage rows carry
``required_event="file_view"`` vs ``actual_event="post_edit"`` -- an EVENT compared
against a SURFACE, which would read as "late" and be meaningless.)

Usage:
    python scripts/swebench/gt_feature_verdicts.py <artifacts_dir> [--json]
    python scripts/swebench/gt_feature_verdicts.py --run <run_id> [--json]

``--run`` reuses the download cache at ``D:/tmp/gt_run_check/<run_id>/`` when it
exists, then falls back to ``D:/tmp/gtrun4/``.  Either way the directory is walked
recursively for ``gt_runtime_ledger_*.jsonl``, so per-task subdirectories at any
depth work.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


# ---------------------------------------------------------------- bootstrap ---

def _bootstrap_import_path() -> Path:
    """Put the repo's ``src`` and ``artifact_deepswe`` on ``sys.path``."""
    repo = Path(__file__).resolve().parents[2]
    for sub in ("src", "artifact_deepswe"):
        candidate = repo / sub
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    return repo


REPO_ROOT = _bootstrap_import_path()


# --------------------------------------------------------------- authorities ---

# Mistake-gated: the trigger is an agent MISTAKE or a rare task shape, so
# TRIGGER-ABSENT is the expected reading on a clean trajectory.  Exactly the six
# FACT rows whose decision context is an error/recovery/completion boundary or a
# net-new-file shape; their CAP byte owners inherit the gate through
# ``_CAP_FACT_BINDING`` (computed, not listed).
_MISTAKE_GATED_FACTS: dict[str, str] = {
    "covering_red": "needs an observed failure with a covering repository test",
    "newfile_precedent": "needs the task to require a net-new file",
    "recovery": "needs the agent to stall / loop / collapse coherence",
    "signature_delta": "needs the agent to change a signature that has callers",
    "submit_refusal": "needs the agent to submit with an unresolved observed RED",
    "syntax_result": "needs the agent to write a syntax error",
}

# Reason classes.  Reason-first, then outcome: the OUTCOME alone cannot decide,
# because e.g. ``suppressed_hidden_only`` carries both "the trigger evaluated
# false" (correct-quiet) and "a referee withheld a real payload" (arbitration).

# The producer ran and there was NOTHING to deliver -> correct-quiet, not a failure.
_NO_EVIDENCE_REASONS = frozenset(
    {
        "plan_none_produced",   # verify.horizon: no covering test exists to execute
        "edit_opportunity",     # edit.syntax denominator marker (an edit happened)
        "clean",                # submit/completion gate ran, nothing to refuse
    }
)
_NO_EVIDENCE_PREFIXES = ("trigger_false",)  # e.g. trigger_false:clean_exit|checkers=...

# The producer could not run / could not render -> a real defect, candidate failure.
_DEFECT_PREFIXES = (
    "engine_import_unavailable",
    "checker_raised",
    "render_failed",
    "provider_failed",
    "suppressed_ack_failure",
)

# Referee arbitration: evidence EXISTED and a self-governing referee withheld it on
# purpose (novelty / dedup / dose / step-behind / provenance / gate).  Legitimate --
# explicitly NOT a delivery failure.
_ARBITRATION_OUTCOMES = frozenset({"suppressed_duplicate", "suppressed_budget"})

# Chars<=0 bookkeeping rows (ACK receipts, internal-only telemetry).  Neither
# evidence-withheld nor a failure.
_TELEMETRY_REASONS = frozenset({"ss_ack"})

_WRONG_PHASE_OUTCOMES = frozenset({"suppressed_wrong_phase"})
_PRODUCED_OUTCOMES = frozenset({"produced", "eligible"})

# A GATE DECISION outcome.  If a layer the map cannot resolve emits one of these,
# some gate-owned feature among the 17 acted and its verdict is UNDERSTATED here.
# Mechanical, not a hand-list of layers: the tool refuses to guess the owner and
# instead reports that its own attribution is incomplete.
_GATE_DECISION_OUTCOMES = frozenset(
    {"bounce_once", "allow", "submit_clean", "refused", "blocked", "provider_failed"}
)

_VERDICT_FIRED = "FIRED"
_VERDICT_ABSENT = "TRIGGER-ABSENT"
_VERDICT_FAILURE = "DELIVERY-FAILURE"

_RUN_CACHE_ROOTS = ("D:/tmp/gt_run_check/{run}", "D:/tmp/gtrun4")


@dataclass
class FeatureRow:
    feature_id: str
    kind: str                       # FACT | CAP
    bound_fact: str                 # CAP -> its FACT; FACT -> itself
    contracted_boundary: str        # FeatureContract.commitment_boundary (EVENT vocab)
    registry_surface: str
    mistake_gated: bool
    gate_note: str
    verdict: str = ""
    delivered: int = 0
    delivered_chars: int = 0
    tasks_fired: set[str] = field(default_factory=set)
    attribution: str = "none"
    on_time: str = ""
    seen_event_types: Counter = field(default_factory=Counter)
    reason_classes: Counter = field(default_factory=Counter)
    reason_detail: Counter = field(default_factory=Counter)
    downgraded: int = 0             # outcome=delivered but chars<=0 (NOT a delivery)
    evidence: str = ""
    inherited: bool = False         # CAP counts copied from its bound FACT


def load_feature_universe() -> list[FeatureRow]:
    """The 17 DIRECT rows, straight from the runtime's canonical contracts."""
    from groundtruth.runtime import fact_registry
    from groundtruth.runtime.reasoning_runtime import (
        _CAP_FACT_BINDING,
        _FACT_DECISION_CONTRACTS,
        feature_contract_for,
    )

    rows: list[FeatureRow] = []
    for feature_id in sorted(_FACT_DECISION_CONTRACTS):
        rows.append(_build_row(feature_id, "FACT", feature_id, feature_contract_for,
                               fact_registry))
    for feature_id in sorted(_CAP_FACT_BINDING):
        rows.append(_build_row(feature_id, "CAP", _CAP_FACT_BINDING[feature_id],
                               feature_contract_for, fact_registry))
    if len(rows) != 17:
        raise SystemExit(
            f"feature universe drifted: expected 17 DIRECT rows, got {len(rows)}"
        )
    return rows


def _build_row(feature_id: str, kind: str, bound_fact: str, contract_for,
               fact_registry) -> FeatureRow:
    contract = contract_for(feature_id)
    if contract is None:
        raise SystemExit(f"no feature contract for {feature_id!r}")
    registration = fact_registry.REGISTRY.get(bound_fact)
    return FeatureRow(
        feature_id=feature_id,
        kind=kind,
        bound_fact=bound_fact,
        contracted_boundary=contract.commitment_boundary,
        registry_surface=getattr(registration, "surface", "?"),
        mistake_gated=bound_fact in _MISTAKE_GATED_FACTS,
        gate_note=_MISTAKE_GATED_FACTS.get(bound_fact, ""),
    )


# ------------------------------------------------------------------- ledgers ---

def resolve_artifacts_dir(args: argparse.Namespace) -> Path:
    if args.artifacts_dir:
        path = Path(args.artifacts_dir)
        if not path.is_dir():
            raise SystemExit(f"not a directory: {path}")
        return path
    candidates = [Path(t.format(run=args.run)) for t in _RUN_CACHE_ROOTS]
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.rglob("gt_runtime_ledger_*.jsonl")):
            # A cache root that does not carry the run id cannot prove it holds THIS
            # run.  Serve it (the caller asked for the fallback) but never let it be
            # mistaken for run-keyed artifacts.
            if str(args.run) not in str(candidate):
                sys.stderr.write(
                    f"WARNING: {candidate} is not keyed by run {args.run}; its "
                    "artifacts may belong to a DIFFERENT run. Verify before citing.\n"
                )
            return candidate
    raise SystemExit(
        "no ledgers found for run "
        f"{args.run} in: {', '.join(str(c) for c in candidates)}"
    )


def iter_ledger_rows(root: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(task_id, row)`` for every ledger line under ``root``."""
    for path in sorted(root.rglob("gt_runtime_ledger_*.jsonl")):
        task_id = path.stem[len("gt_runtime_ledger_"):] or path.parent.name
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict):
                    yield task_id, row


# --------------------------------------------------------------- attribution ---

def attribute_row(row: dict[str, Any], layer_to_fact, fact_identity_for_layer,
                  delivery_facts: frozenset[str]) -> tuple[str | None, str]:
    """Return ``(fact_class, how)`` for a ledger row, or ``(None, reason)``.

    Correct-or-quiet: an unmapped layer resolves to nothing rather than a guess.
    """
    stamped = row.get("fact_class")
    if isinstance(stamped, str) and stamped in delivery_facts:
        return stamped, "row.fact_class"
    layer = str(row.get("layer") or "").strip()
    mapped, _boundary = fact_identity_for_layer(layer)
    if mapped and mapped in delivery_facts:
        return mapped, "layer_map"
    if mapped:
        return None, f"layer_map->{mapped}(not a delivery FACT)"
    return None, "unmapped_layer"


def cap_owners_in_row(row: dict[str, Any], lane_owners: dict[str, str],
                      cap_ids: frozenset[str]) -> set[str]:
    """CAP byte owners this row NAMES (never inferred from the bound FACT)."""
    owners: set[str] = set()
    member = row.get("profile_member")
    if isinstance(member, str) and member in cap_ids:
        owners.add(member)
    lane = lane_owners.get(str(row.get("layer") or "").strip())
    if lane in cap_ids:
        owners.add(lane)
    for entry in row.get("feature_ids") or ():
        if isinstance(entry, dict):
            fid = entry.get("feature_id")
            if isinstance(fid, str) and fid in cap_ids:
                owners.add(fid)
    return owners


def classify_reason(row: dict[str, Any]) -> tuple[str, str]:
    """Return ``(class, detail)`` for a NON-delivered row."""
    outcome = str(row.get("outcome") or "")
    reason = str(row.get("reason") or "")
    head = reason.split(":", 1)[0].split("|", 1)[0]

    if reason in _NO_EVIDENCE_REASONS or head in _NO_EVIDENCE_REASONS:
        return "no_evidence", reason or outcome
    if any(reason.startswith(p) for p in _NO_EVIDENCE_PREFIXES):
        return "no_evidence", head
    if any(reason.startswith(p) for p in _DEFECT_PREFIXES):
        return "defect", reason
    if outcome in _WRONG_PHASE_OUTCOMES:
        return "wrong_phase", reason or outcome
    if reason in _TELEMETRY_REASONS:
        return "telemetry", reason
    if outcome in _ARBITRATION_OUTCOMES or reason.startswith("ss_"):
        return "arbitration", reason or outcome
    if outcome == "suppressed_internal_only":
        return "telemetry", reason or outcome
    if outcome in _PRODUCED_OUTCOMES:
        return "produced", reason or outcome
    if outcome == "delivered":
        return "downgraded", f"{outcome}(chars<=0)"
    if outcome in {"submit_clean", "allow"}:
        return "no_evidence", reason or outcome
    return "other", reason or outcome


# ------------------------------------------------------------------ on-time ---

def observed_event(row: dict[str, Any], events: frozenset[str]) -> str | None:
    """An observed boundary in EVENT vocabulary, or ``None``.

    ``event_type`` and ``surface`` are deliberately NOT consulted: they are
    different vocabularies and comparing them to ``contracted_boundary``
    manufactures a false "off-boundary" verdict.
    """
    for key in ("observed_boundary", "actual_event"):
        value = row.get(key)
        if isinstance(value, str) and value in events:
            return value
    return None


# ---------------------------------------------------------------- evaluation ---

def evaluate(root: Path) -> dict[str, Any]:
    from groundtruth.runtime import fact_registry
    from groundtruth.runtime import feature_lineage
    from gt_mini_patch import (  # type: ignore[import-not-found]
        _LANE_PROFILE_MEMBER_OWNERS,
        _LAYER_TO_FACT_CLASS,
        _fact_identity_for_layer,
    )

    features = load_feature_universe()
    facts = {f.feature_id: f for f in features if f.kind == "FACT"}
    caps = [f for f in features if f.kind == "CAP"]
    cap_ids = frozenset(feature_lineage.CAP_BYTE_OWNER_IDS)
    delivery_facts = frozenset(facts)
    events = fact_registry.EVENTS

    tasks: set[str] = set()
    total_rows = 0
    unattributed: Counter = Counter()
    unattributed_delivered: Counter = Counter()
    unresolved_gate: Counter = Counter()
    cap_direct: dict[str, dict[str, Any]] = {
        cap.feature_id: {"delivered": 0, "tasks": set(), "how": set(),
                         "event_types": Counter()}
        for cap in caps
    }
    boundary_stamped = 0
    on_time_hits: Counter = Counter()

    for task_id, row in iter_ledger_rows(root):
        tasks.add(task_id)
        total_rows += 1
        outcome = str(row.get("outcome") or "")
        try:
            chars = int(row.get("chars_delivered") or 0)
        except (TypeError, ValueError):
            chars = 0
        is_delivery = outcome == "delivered" and chars > 0
        event_type = str(row.get("event_type") or "") or "(none)"

        # CAP byte owners are credited ONLY when the row names them.
        for owner in cap_owners_in_row(row, _LANE_PROFILE_MEMBER_OWNERS, cap_ids):
            if owner not in cap_direct:
                continue
            how = []
            if row.get("profile_member") == owner:
                how.append("profile_member")
            if _LANE_PROFILE_MEMBER_OWNERS.get(str(row.get("layer") or "")) == owner:
                how.append("lane_owner")
            if any(isinstance(e, dict) and e.get("feature_id") == owner
                   for e in row.get("feature_ids") or ()):
                how.append("feature_ids")
            if is_delivery:
                cap_direct[owner]["delivered"] += 1
                cap_direct[owner]["tasks"].add(task_id)
                cap_direct[owner]["event_types"][event_type] += 1
                cap_direct[owner]["how"].update(how)

        fact_class, how = attribute_row(row, _LAYER_TO_FACT_CLASS,
                                        _fact_identity_for_layer, delivery_facts)
        if fact_class is None:
            key = f"{row.get('layer')}|{outcome}|{how}"
            unattributed[key] += 1
            if is_delivery:
                unattributed_delivered[f"{row.get('layer')}|{event_type}"] += 1
            if outcome in _GATE_DECISION_OUTCOMES:
                unresolved_gate[f"{row.get('layer')}|{outcome}"] += 1
            continue

        target = facts[fact_class]
        if is_delivery:
            target.delivered += 1
            target.delivered_chars += chars
            target.tasks_fired.add(task_id)
            target.seen_event_types[event_type] += 1
            contracted = row.get("contracted_boundary")
            if isinstance(contracted, str) and contracted:
                boundary_stamped += 1
                seen = observed_event(row, events)
                if seen is None:
                    on_time_hits[(fact_class, "no_event_vocab_observation")] += 1
                elif seen == contracted:
                    on_time_hits[(fact_class, "on_time")] += 1
                else:
                    on_time_hits[(fact_class, f"off_boundary:{seen}")] += 1
        else:
            klass, detail = classify_reason(row)
            target.reason_classes[klass] += 1
            target.reason_detail[f"{klass}:{detail}"] += 1
            if klass == "downgraded":
                target.downgraded += 1

    # ---- FACT verdicts -------------------------------------------------------
    for fact in facts.values():
        _decide(fact, on_time_hits, boundary_stamped)

    # ---- CAP verdicts --------------------------------------------------------
    for cap in caps:
        direct = cap_direct[cap.feature_id]
        bound = facts[cap.bound_fact]
        if direct["delivered"] > 0:
            cap.verdict = _VERDICT_FIRED
            cap.delivered = direct["delivered"]
            cap.tasks_fired = set(direct["tasks"])
            cap.seen_event_types = Counter(direct["event_types"])
            how = "+".join(sorted(direct["how"])) or "row"
            cap.attribution = f"byte-owner lineage: {how}"
            cap.on_time = bound.on_time
            cap.evidence = ""
            continue
        # No row names this CAP.  Fall back to its bound FACT, and SAY SO.  Every
        # inherited cell is marked '^' in the table so a bound-FACT count is never
        # read as proof that THIS capability owned the bytes.
        cap.verdict = bound.verdict
        cap.inherited = True
        cap.delivered = bound.delivered
        cap.delivered_chars = bound.delivered_chars
        cap.tasks_fired = set(bound.tasks_fired)
        cap.seen_event_types = Counter(bound.seen_event_types)
        cap.on_time = bound.on_time
        cap.reason_classes = Counter(bound.reason_classes)
        cap.reason_detail = Counter(bound.reason_detail)
        cap.attribution = f"bound-FACT {cap.bound_fact} (no byte-owner lineage row)"
        if bound.verdict == _VERDICT_FIRED:
            cap.evidence = (
                f"bytes reached the model as {cap.bound_fact} "
                f"({bound.delivered} delivery/ies) but no ledger row names this CAP "
                "-- CAP-specific attribution UNPROVEN, not a failure"
            )
        else:
            cap.evidence = bound.evidence

    return {
        "root": str(root),
        "tasks": sorted(tasks),
        "total_rows": total_rows,
        "features": features,
        "unattributed": unattributed,
        "unattributed_delivered": unattributed_delivered,
        "unresolved_gate": unresolved_gate,
        "boundary_stamped": boundary_stamped,
        "on_time_hits": on_time_hits,
    }


def _decide(fact: FeatureRow, on_time_hits: Counter, boundary_stamped: int) -> None:
    classes = fact.reason_classes
    if fact.delivered > 0:
        fact.verdict = _VERDICT_FIRED
        fact.attribution = "layer/fact_class rows"
        bits = []
        if classes.get("arbitration"):
            bits.append(f"+{classes['arbitration']} arbitrated")
        if classes.get("produced"):
            bits.append(f"+{classes['produced']} produced")
        if fact.downgraded:
            bits.append(f"+{fact.downgraded} delivered-but-chars<=0 (NOT a delivery)")
        fact.evidence = "; ".join(bits)
    elif classes.get("wrong_phase") or classes.get("defect"):
        fact.verdict = _VERDICT_FAILURE
        fact.attribution = "layer/fact_class rows"
        fact.evidence = _top_detail(fact, ("wrong_phase", "defect"))
    elif ((classes.get("produced") or classes.get("downgraded"))
            and not classes.get("arbitration")):
        fact.verdict = _VERDICT_FAILURE
        fact.attribution = "layer/fact_class rows"
        fact.evidence = (
            "produced/eligible (or delivered with chars<=0) but no real delivery and "
            "no arbitration explaining it: "
            + _top_detail(fact, ("produced", "downgraded"))
        )
    elif classes.get("arbitration"):
        # Evidence EXISTED and a referee withheld it.  Explicitly NOT a delivery
        # failure (the referee is doing its job), and not literally "trigger absent"
        # either -- the three-verdict taxonomy has no fourth bucket, so it is
        # counted as TRIGGER-ABSENT and labelled so no reader is misled.
        fact.verdict = _VERDICT_ABSENT
        fact.attribution = "layer/fact_class rows"
        fact.evidence = "ARBITRATED (evidence produced, referee withheld -- NOT a " \
            "delivery failure): " + _top_detail(fact, ("arbitration",))
    else:
        fact.verdict = _VERDICT_ABSENT
        fact.attribution = "layer/fact_class rows" if classes else "no rows"
        if classes:
            fact.evidence = _top_detail(
                fact, ("no_evidence", "telemetry", "downgraded", "other")
            )
        else:
            fact.evidence = "no ledger row for this feature's producer layer(s)"

    # On-time is scored ONLY from contracted_boundary; anything else is a guess.
    hits = {k[1]: v for k, v in on_time_hits.items() if k[0] == fact.feature_id}
    if not hits:
        fact.on_time = "NOT-EVALUABLE(no contracted_boundary)"
    elif set(hits) == {"on_time"}:
        fact.on_time = f"ON-TIME {hits['on_time']}/{hits['on_time']}"
    elif set(hits) == {"no_event_vocab_observation"}:
        fact.on_time = "NOT-EVALUABLE(no EVENT-vocab observation)"
    else:
        total = sum(hits.values())
        fact.on_time = f"{hits.get('on_time', 0)}/{total} on-time; " + ",".join(
            f"{k}x{v}" for k, v in sorted(hits.items()) if k != "on_time"
        )


def _top_detail(fact: FeatureRow, classes: tuple[str, ...], limit: int = 3) -> str:
    items = [
        (detail, count)
        for detail, count in fact.reason_detail.items()
        if detail.split(":", 1)[0] in classes
    ]
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{d} x{c}" for d, c in items[:limit]) or "-"


# -------------------------------------------------------------------- render ---

def render_text(result: dict[str, Any]) -> str:
    features: list[FeatureRow] = result["features"]
    tasks: list[str] = result["tasks"]
    out: list[str] = []
    out.append(f"GT 17-DIRECT FEATURE VERDICTS  |  artifacts: {result['root']}")
    out.append(
        f"tasks: {len(tasks)}  ledger rows: {result['total_rows']}  "
        f"rows carrying contracted_boundary: {result['boundary_stamped']}"
    )
    out.append("")

    header = (
        f"{'FEATURE':<20} {'KIND':<4} {'CONTRACTED':<14} {'VERDICT':<16} "
        f"{'DELIV':>6} {'TASK':>5}  {'BOUNDARIES SEEN (ledger event_type)':<46} "
        f"ON-TIME"
    )
    out.append(header)
    out.append("-" * len(header))
    for f in sorted(features, key=lambda r: (r.kind, r.feature_id)):
        seen = ", ".join(
            f"{k}x{v}" for k, v in sorted(f.seen_event_types.items()) if v
        ) or "-"
        if len(seen) > 46:
            seen = seen[:45] + "~"
        gate = "*" if f.mistake_gated else " "
        mark = "^" if f.inherited else " "
        out.append(
            f"{f.feature_id + gate:<20} {f.kind:<4} {f.contracted_boundary:<14} "
            f"{f.verdict:<16} {str(f.delivered) + mark:>6} "
            f"{len(f.tasks_fired):>2}/{len(tasks):<2}  {seen:<46} {f.on_time}"
        )
    out.append("")
    out.append("* = MISTAKE-GATED: fires only on an agent mistake or a rare task shape.")
    out.append("  TRIGGER-ABSENT on these is CORRECT-QUIET evidence of a clean run, "
               "not a broken feature.")
    out.append("^ = counts INHERITED from the bound FACT; no ledger row names this CAP "
               "byte owner.")
    out.append("CONTRACTED is EVENT vocabulary (fact_registry.EVENTS); BOUNDARIES SEEN is "
               "the ledger's own")
    out.append("  event_type string (mixed surface/event/producer names). They are NOT "
               "comparable -- see ON-TIME.")
    out.append("")

    out.append("WHY (every non-FIRED row, plus caveats on FIRED rows):")
    for f in sorted(features, key=lambda r: (r.verdict != _VERDICT_FAILURE,
                                             r.verdict, r.kind, r.feature_id)):
        if f.verdict == _VERDICT_FIRED and not f.evidence and f.attribution.startswith(
                ("layer", "byte-owner")):
            continue
        gate = f"  [mistake-gated: {f.gate_note}]" if f.mistake_gated else ""
        out.append(f"  {f.feature_id} ({f.kind}) -> {f.verdict}")
        out.append(f"      attribution: {f.attribution}{gate}")
        if f.evidence:
            out.append(f"      evidence   : {f.evidence}")
    out.append("")

    counts = Counter(f.verdict for f in features)
    fired = counts.get(_VERDICT_FIRED, 0)
    absent = counts.get(_VERDICT_ABSENT, 0)
    failed = counts.get(_VERDICT_FAILURE, 0)
    out.append(
        f"SUMMARY: {fired} FIRED / {absent} TRIGGER-ABSENT / {failed} DELIVERY-FAILURE"
        f" out of {len(features)}"
    )
    gated_absent = [f.feature_id for f in features
                    if f.verdict == _VERDICT_ABSENT and f.mistake_gated]
    if gated_absent:
        out.append(
            f"  of the {absent} TRIGGER-ABSENT, {len(gated_absent)} are mistake-gated "
            f"(correct-quiet, NOT failures): {', '.join(sorted(gated_absent))}"
        )
    arbitrated = [f.feature_id for f in features
                  if f.verdict == _VERDICT_ABSENT
                  and f.evidence.startswith("ARBITRATED")]
    if arbitrated:
        out.append(
            f"  {len(arbitrated)} counted TRIGGER-ABSENT are ARBITRATED (evidence "
            f"produced, referee withheld -- not delivery failures): "
            f"{', '.join(sorted(arbitrated))}"
        )
    inherited = [f.feature_id for f in features
                 if f.verdict == _VERDICT_FIRED and f.attribution.startswith("bound-FACT")]
    if inherited:
        out.append(
            f"  {len(inherited)} FIRED rest on bound-FACT delivery, NOT byte-owner "
            f"lineage (CAP-specific attribution unproven): {', '.join(sorted(inherited))}"
        )
    if result["boundary_stamped"] == 0:
        out.append(
            "  ON-TIME: NOT EVALUABLE for all 17 -- no ledger row carries "
            "`contracted_boundary` (pre-2026-07-26 run). `surface`/`event_type` are "
            "different vocabularies and were NOT substituted."
        )
    gate = result["unresolved_gate"]
    if gate:
        out.append(
            "  CAVEAT -- VERDICTS MAY BE UNDERSTATED: a gate DECISION was emitted by a "
            "layer the layer->fact map cannot resolve, so the gate-owned feature(s) "
            "among the 17 may read TRIGGER-ABSENT when the trigger did occur: "
            + ", ".join(f"{k} x{v}" for k, v in sorted(gate.items()))
        )
    out.append("")

    un_deliv = result["unattributed_delivered"]
    if un_deliv:
        out.append(
            f"UNATTRIBUTED DELIVERIES ({sum(un_deliv.values())} delivered rows whose "
            "layer is not in _LAYER_TO_FACT_CLASS -- not counted for any of the 17):"
        )
        for key, count in un_deliv.most_common():
            out.append(f"  {count:>4}  {key}")
    interesting = Counter({
        k: v for k, v in result["unattributed"].items()
        if not k.startswith("control.participation|")
        and "|evaluated|" not in k
    })
    gate_layers = {k: v for k, v in interesting.items()
                   if k.split("|", 1)[0] in {"completion_cert", "submit_gate",
                                             "change_surface", "patch_delta"}}
    if gate_layers:
        out.append("  gate-layer rows the layer map does not resolve (coverage gap):")
        for key, count in sorted(gate_layers.items()):
            out.append(f"  {count:>4}  {key}")
    return "\n".join(out)


def render_json(result: dict[str, Any]) -> str:
    features: list[FeatureRow] = result["features"]
    payload = {
        "artifacts_dir": result["root"],
        "tasks": result["tasks"],
        "total_ledger_rows": result["total_rows"],
        "rows_with_contracted_boundary": result["boundary_stamped"],
        "features": [
            {
                "feature": f.feature_id,
                "kind": f.kind,
                "bound_fact": f.bound_fact,
                "contracted_boundary": f.contracted_boundary,
                "registry_surface": f.registry_surface,
                "mistake_gated": f.mistake_gated,
                "mistake_gate": f.gate_note,
                "verdict": f.verdict,
                "delivered_rows": f.delivered,
                "delivered_chars": f.delivered_chars,
                "tasks_fired": sorted(f.tasks_fired),
                "boundaries_seen_event_type": dict(f.seen_event_types),
                "on_time": f.on_time,
                "attribution": f.attribution,
                "counts_inherited_from_bound_fact": f.inherited,
                "evidence": f.evidence,
                "non_delivery_reason_classes": dict(f.reason_classes),
                "non_delivery_reason_detail": dict(f.reason_detail),
            }
            for f in sorted(features, key=lambda r: (r.kind, r.feature_id))
        ],
        "summary": {
            verdict: sum(1 for f in features if f.verdict == verdict)
            for verdict in (_VERDICT_FIRED, _VERDICT_ABSENT, _VERDICT_FAILURE)
        },
        "unattributed_delivered_rows": dict(result["unattributed_delivered"]),
        "unresolved_gate_decisions": dict(result["unresolved_gate"]),
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-feature verdicts for the 17 DIRECT GroundTruth features.",
    )
    parser.add_argument("artifacts_dir", nargs="?",
                        help="directory holding one subdirectory per task")
    parser.add_argument("--run", help="run id; reuses D:/tmp/gt_run_check/<run>/")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if not args.artifacts_dir and not args.run:
        parser.error("give an artifacts_dir or --run <id>")

    root = resolve_artifacts_dir(args)
    result = evaluate(root)
    text = render_json(result) if args.json else render_text(result)
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
