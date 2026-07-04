"""Truthful hook visibility/blocking normalization for reports."""

from __future__ import annotations

from typing import Any


def normalize_hook_truth(record: dict[str, Any]) -> dict[str, bool]:
    """Return stable hook truthfulness fields for mixed old/new telemetry."""
    explicit = {
        "hook_logged": record.get("hook_logged"),
        "hook_visible_to_agent": record.get("hook_visible_to_agent"),
        "hook_blocked": record.get("hook_blocked"),
        "final_audit_only": record.get("final_audit_only"),
    }
    if all(isinstance(value, bool) for value in explicit.values()):
        return {key: bool(value) for key, value in explicit.items()}

    output = record.get("output")
    has_output = isinstance(output, str) and bool(output.strip())
    hook_present = bool(record.get("hook") or record.get("endpoint") or record.get("gt_runtime"))
    return {
        "hook_logged": hook_present or bool(record),
        "hook_visible_to_agent": bool(has_output),
        "hook_blocked": bool(record.get("blocked") or record.get("hook_blocked", False)),
        "final_audit_only": bool(record.get("final_audit_only", False)),
    }
