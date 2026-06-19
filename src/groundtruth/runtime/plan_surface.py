"""Agent-facing v7 plan projection and delivery-quality telemetry."""

from __future__ import annotations

import json
import re
from typing import Any

MAX_AGENT_PLAN_CHARS = 3500


def compact_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the compact, ranked plan shape safe for agent-facing tools."""
    return {
        "task_id": plan.get("task_id", "unknown"),
        "confidence": plan.get("confidence", 0),
        "abstain_reason": plan.get("abstain_reason", ""),
        "agent_focus_files": _take_list(plan.get("agent_focus_files"), 3),
        "contract_lines": _take_list(plan.get("contract_lines"), 2),
        "constraints": _take_list(plan.get("constraints"), 3),
        "expected_side_files": _take_list(plan.get("expected_side_files"), 3),
        "full_plan_available": True,
    }


def served_plan_record(plan: dict[str, Any], *, full: bool, surface: str) -> dict[str, Any]:
    """Describe the actual payload served to an agent-facing plan surface."""
    payload = plan if full else compact_plan(plan)
    rendered = json.dumps(payload, sort_keys=True)
    return {
        "surface": surface,
        "full": bool(full),
        "agent_facing": True,
        "char_count": len(rendered),
        "served_keys": sorted(payload.keys()),
        "broad_full_plan_json": bool(full),
    }


def usable_delivery_record(
    *,
    transport_delivered: bool,
    brief_chars: int,
    agent_focus_files: list[Any] | None,
    brief_text: str = "",
    first_message_chars: int | None = None,
    broad_full_plan_default: bool = False,
) -> dict[str, Any]:
    """Evaluate whether delivered GT context is compact enough to be usable."""
    focus_count = len(agent_focus_files or [])
    brief_file_mentions_count = len(_file_mentions(brief_text))
    failure_reasons: list[str] = []
    if not transport_delivered:
        failure_reasons.append("transport_not_delivered")
    if focus_count > 3:
        failure_reasons.append("too_many_focus_files")
    if brief_chars > MAX_AGENT_PLAN_CHARS:
        failure_reasons.append("brief_too_large")
    if broad_full_plan_default:
        failure_reasons.append("broad_full_plan_default")
    return {
        "transport_delivered": bool(transport_delivered),
        "brief_chars": int(brief_chars),
        "first_message_chars": first_message_chars,
        "agent_focus_count": focus_count,
        "brief_file_mentions_count": brief_file_mentions_count,
        "usable_delivery_ok": not failure_reasons,
        "failure_reasons": failure_reasons,
    }


def _take_list(value: Any, limit: int) -> list[Any]:
    return list(value[:limit]) if isinstance(value, list) else []


def _file_mentions(text: str) -> set[str]:
    if not text:
        return set()
    pattern = r"\b[\w./-]+\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|kt|c|h|cc|cpp|hpp|rb|php|cs)\b"
    return {match.replace("\\", "/").lstrip("./") for match in re.findall(pattern, text)}
