#!/usr/bin/env python3
"""GT Behavioral Impact — measures action transitions around GT deliveries.

For every GT block delivered to the agent (<gt-evidence>, <gt-contract>,
<gt-nudge>, <gt-scope>, <gt-verify>, <gt-obligations>), this module captures:
  - the agent's action TYPE before the delivery
  - the agent's action TYPE after the delivery
  - whether the type CHANGED (a behavioral pivot)

A PIVOT means the classified action type changed after delivery. It is a
diagnostic temporal association, not evidence that GT caused the action.
Consumption and causality require receipt and matched/shadow evidence.

Usage:
  python gt_behavioral_impact.py <trajectory.json> [--out impact.json]

Reads the pier/mini-swe-agent trajectory (messages list with role/content/
tool_calls). Outputs per-delivery records + aggregate.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from consumption_ledger import (  # noqa: E402  (#43 physical delivery substrates)
    MODEL_VISIBLE_SOURCES as _MODEL_VISIBLE_SOURCES,
)


# Action type classifier — what is the agent doing?
_EDIT_PATTERNS = re.compile(
    r"str_replace|create.*file|insert|write.*file|cat\s*>|sed\s+-i|"
    r"patch\s|tee\s|echo\s.*>>|apply_patch|git\s+apply",
    re.I,
)
_TEST_PATTERNS = re.compile(
    r"pytest|cargo\s+test|go\s+test|npm\s+test|yarn\s+test|vitest|jest|"
    r"make\s+test|tox|unittest|runtests|run_tests|\.test\.|test.*runner",
    re.I,
)
_SEARCH_PATTERNS = re.compile(
    r"grep\s+-|rg\s|find\s.*-name|ag\s|ack\s|grep\s.*def\s|"
    r"grep\s.*class\s|grep\s.*import|locate\s",
    re.I,
)
_READ_PATTERNS = re.compile(
    r"cat\s|head\s|tail\s|less\s|more\s|view\s|open\s.*\.|\bls\b|"
    r"bat\s|sed\s+-n|awk\s",
    re.I,
)
_BUILD_PATTERNS = re.compile(
    r"cargo\s+build|cargo\s+check|go\s+build|tsc\b|npm\s+run\s+build|"
    r"make\b|cmake|gcc|g\+\+|rustc|javac|mvn\s+compile",
    re.I,
)
_SUBMIT_PATTERNS = re.compile(
    r"submit|COMPLETE_TASK|final.*output|git\s+diff\s*>|done",
    re.I,
)

# GT block tags we track
_GT_TAGS = re.compile(
    r"<gt-(evidence|contract|scope|nudge|verify|obligations)"
)

_DELIVERY_CLASS = {
    "evidence": "caller_contract",
    "contract": "caller_contract",
    "l3b.evidence": "caller_contract",
    "l3b.contract": "caller_contract",
    "l3.contract": "caller_contract",
    "scope": "localization",
    "consensus.scope_map": "localization",
    "consensus.scope": "localization",
    "edit.syntax": "syntax_result",
    "semantic_drift": "cochange_prior",
    "nudge": "recovery",
    "detect.coherence": "recovery",
    "detect.loop": "recovery",
    "obligations": "obligations",
    "spec.obligation": "obligations",
    "obligation.resurface": "obligations",
}


def _delivery_class(layer: str, feature_lineage: object = None) -> str:
    """Stable metric class for a tagged kind or native runtime-ledger layer."""
    normalized = (layer or "unknown").removeprefix("ga.")
    if (
        isinstance(feature_lineage, dict)
        and feature_lineage.get("schema") == "gt.feature_lineage.v1"
        and feature_lineage.get("producer_registration_match") is True
        and isinstance(feature_lineage.get("fact_class"), str)
        and feature_lineage.get("fact_class")
        and not (
            feature_lineage.get("fact_class") == "covering_red"
            and normalized.startswith("verify.horizon.")
            and (
                normalized != "verify.horizon.executed"
                or feature_lineage.get("runtime_producer_id") != "covering_runner"
            )
        )
    ):
        return str(feature_lineage["fact_class"])
    if normalized.startswith("gateway."):
        normalized = normalized.split(".", 1)[1]
    return _DELIVERY_CLASS.get(normalized, normalized)


def classify_action(text: str) -> str:
    """Classify an agent action into a behavioral type."""
    if not text:
        return "unknown"
    if _SUBMIT_PATTERNS.search(text):
        return "submitting"
    if _TEST_PATTERNS.search(text):
        return "testing"
    if _EDIT_PATTERNS.search(text):
        return "editing"
    if _BUILD_PATTERNS.search(text):
        return "building"
    if _SEARCH_PATTERNS.search(text):
        return "searching"
    if _READ_PATTERNS.search(text):
        return "reading"
    return "thinking"


def _extract_gt_tags(content: str) -> list[str]:
    """Extract all GT delivery tag types from observation content."""
    return _GT_TAGS.findall(content)


def analyze_trajectory(trajectory: dict, *, consumption_ledger: dict | None = None) -> dict:
    """Analyze a pier/mini-swe-agent trajectory for GT behavioral impact.

    Returns {deliveries: [...], summary: {...}}.
    """
    messages = trajectory.get("messages", []) or []

    # Build a timeline: [(role, action_type, gt_tags, step_num), ...]
    timeline: list[dict] = []
    step = 0

    for msg_index, m in enumerate(messages):
        role = m.get("role", "")
        content = m.get("content") or ""

        if role == "assistant":
            step += 1
            # The action is in tool_calls (the command) + content (the thought)
            cmd = json.dumps(m.get("tool_calls") or "")
            full_text = cmd + " " + (content if isinstance(content, str) else "")
            action_type = classify_action(full_text)
            timeline.append({
                "role": "assistant",
                "msg_index": msg_index,
                "step": step,
                "action_type": action_type,
                "text_preview": full_text[:120],
            })

        elif role in ("tool", "user"):
            gt_tags = _extract_gt_tags(content) if isinstance(content, str) else []
            timeline.append({
                "role": "observation",
                "msg_index": msg_index,
                "step": step,
                "gt_tags": gt_tags,
                "has_gt": bool(gt_tags),
                "text_preview": (content[:120] if isinstance(content, str) else ""),
            })

    # A receipt ledger is authoritative when supplied: it includes tagless native
    # Profile-2 bytes joined to runtime rows by content seal. Tag scanning remains
    # only as a backward-compatible fallback for historical trajectories.
    scan_timeline = timeline
    gt_chars_injected = 0
    recovery_delivered = 0
    recovery_acted = 0
    if consumption_ledger is not None:
        join_required = bool(
            consumption_ledger.get("runtime_ledger_path")
            or consumption_ledger.get("ledger_rows_delivered")
        )
        scan_timeline = [e for e in timeline if e.get("role") == "assistant"]
        for receipt in consumption_ledger.get("entries", []):
            level = int(receipt.get("receipt") or 0)
            # #43: BOTH physical substrates. The capsule is injected at the
            # PROVIDER boundary, so its bytes are model-visible and structurally
            # absent from the trajectory file; a trajectory-only predicate made
            # total_deliveries=0 BY CONSTRUCTION on real deliveries.
            if receipt.get("source") not in _MODEL_VISIBLE_SOURCES or level < 1:
                continue
            if join_required and receipt.get("joined") is not True:
                continue
            home = receipt.get("msg_index")
            if not isinstance(home, int):
                continue
            kind = _delivery_class(str(
                receipt.get("ledger_layer") or receipt.get("kind") or "unknown"
            ), receipt.get("feature_lineage"))
            if kind == "recovery" and level >= 1:
                recovery_delivered += 1
                if level >= 3:
                    recovery_acted += 1
            chars = int(receipt.get("chars") or 0)
            gt_chars_injected += chars
            scan_timeline.append({
                "role": "observation",
                "msg_index": home,
                "step": receipt.get("tool_ordinal") or 0,
                "gt_tags": [kind],
                "has_gt": True,
                "chars": chars,
            })
        scan_timeline.sort(key=lambda e: (
            int(e.get("msg_index", -1)), 0 if e.get("role") == "observation" else 1
        ))

    # Find every GT delivery and pair it with before/after agent actions.
    deliveries = []
    for i, entry in enumerate(scan_timeline):
        if entry.get("role") != "observation" or not entry.get("has_gt"):
            continue

        # Find the last assistant action BEFORE this observation
        action_before = None
        for j in range(i - 1, -1, -1):
            if scan_timeline[j].get("role") == "assistant":
                action_before = scan_timeline[j]
                break

        # Find the next assistant action AFTER this observation
        action_after = None
        for j in range(i + 1, len(scan_timeline)):
            if scan_timeline[j].get("role") == "assistant":
                action_after = scan_timeline[j]
                break

        before_type = action_before["action_type"] if action_before else "none"
        after_type = action_after["action_type"] if action_after else "none"
        pivot = before_type != after_type and after_type != "none"

        delivery = {
            "step": entry["step"],
            "gt_tags": entry["gt_tags"],
            "action_before": before_type,
            "action_after": after_type,
            "pivot": pivot,
            "transition": f"{before_type} -> {after_type}" if pivot else f"{before_type} (no change)",
            "chars": int(entry.get("chars") or 0),
        }
        deliveries.append(delivery)

    # Aggregate
    total = len(deliveries)
    pivots = sum(1 for d in deliveries if d["pivot"])
    # A zero denominator is UNMEASURED, never a measured 0.0: with no delivery
    # there is no pivot opportunity, so "0% of deliveries pivoted" is a
    # manufactured verdict. gt_deep_metrics.py:2100-2104 already nulls on the
    # same predicate and gt_performance_metrics.py:899 marks the metric
    # not-applicable; emitting 0.0 here made the two sides disagree and tripped
    # the workflow behavioral_deep_parity check on EVERY zero-delivery task.
    impact_rate = pivots / total if total > 0 else None
    impact_rate_reason = None if total > 0 else "zero_denominator_no_gt_deliveries"

    # Transition breakdown
    transitions: dict[str, int] = {}
    for d in deliveries:
        t = d["transition"]
        transitions[t] = transitions.get(t, 0) + 1

    # Per-tag-type breakdown
    tag_pivots: dict[str, dict] = {}
    for d in deliveries:
        for tag in d["gt_tags"]:
            if tag not in tag_pivots:
                tag_pivots[tag] = {"total": 0, "pivots": 0}
            tag_pivots[tag]["total"] += 1
            if d["pivot"]:
                tag_pivots[tag]["pivots"] += 1

    summary = {
        "total_deliveries": total,
        "total_pivots": pivots,
        "impact_rate": round(impact_rate, 8) if impact_rate is not None else None,
        # Named UNMEASURED reason for the artifact readers that have no
        # applicability contract of their own. The deep record carries the same
        # fact structurally (metric_applicability.behavioral_impact.impact_rate,
        # built by gt_performance_metrics.build_metric_applicability), so this
        # key is deliberately summary-only: the workflow parity check iterates
        # deep_behavioral.items(), and a key present on both sides would have to
        # be kept byte-equal by hand for no gain.
        "impact_rate_reason": impact_rate_reason,
        "impact_rate_semantics": "diagnostic_action_type_transition",
        "causal_status": "UNMEASURED",
        "gt_tokens_injected": gt_chars_injected if consumption_ledger is not None else None,
        "gt_tokens_injected_units": (
            "rendered_chars_exact" if consumption_ledger is not None else None
        ),
        "gt_tokens_per_pivot": (
            round(gt_chars_injected / pivots, 8)
            if consumption_ledger is not None and pivots > 0 else None
        ),
        "nudge_compliance_rate": (
            round(recovery_acted / recovery_delivered, 8)
            if recovery_delivered else None
        ),
        "nudge_compliance_numerator": recovery_acted,
        "nudge_compliance_denominator": recovery_delivered,
        "transitions": transitions,
        "per_tag": {
            tag: {
                "total": v["total"],
                "pivots": v["pivots"],
                "rate": round(v["pivots"] / v["total"], 8) if v["total"] > 0 else 0.0,
            }
            for tag, v in sorted(tag_pivots.items())
        },
    }
    # Mandatory-metrics contract name. Keep ``per_tag`` as a compatibility
    # alias for existing report readers during migration.
    summary["per_tag_impact"] = summary["per_tag"]

    return {"deliveries": deliveries, "summary": summary}


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <trajectory.json> [--out impact.json]")
        sys.exit(1)

    traj_path = sys.argv[1]
    out_path = None
    if "--out" in sys.argv:
        idx = sys.argv.index("--out")
        if idx + 1 < len(sys.argv):
            out_path = sys.argv[idx + 1]

    with open(traj_path) as f:
        traj = json.load(f)

    try:
        from consumption_ledger import ledger_from_trajectory_path
    except ImportError:
        from scripts.swebench.consumption_ledger import ledger_from_trajectory_path

    receipts = ledger_from_trajectory_path(traj_path)
    result = analyze_trajectory(traj, consumption_ledger=receipts)
    s = result["summary"]

    rate = s["impact_rate"]
    rendered_rate = (
        f"{rate:.1%}" if rate is not None
        else f"UNMEASURED:{s.get('impact_rate_reason') or 'zero_denominator'}"
    )
    print(f"GT Behavioral Impact: {s['total_pivots']}/{s['total_deliveries']} "
          f"deliveries were followed by an action-type pivot ({rendered_rate})")
    print()
    print("Per-tag breakdown:")
    for tag, v in s["per_tag"].items():
        print(f"  <gt-{tag}>: {v['pivots']}/{v['total']} pivots ({v['rate']:.1%})")
    print()
    print("Transitions (most common):")
    for t, c in sorted(s["transitions"].items(), key=lambda x: -x[1])[:10]:
        print(f"  {t}: {c}")

    if out_path:
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nFull report: {out_path}")


if __name__ == "__main__":
    main()
