"""Product-owned graph fact to action translation."""
from __future__ import annotations

import re

from .context_policy import Phase


ACTION_TEMPLATES = {
    "witness_call": "Inspect {sym} at {loc} before changing related code; this caller path is part of the live behavior.",
    "caller_check": "Check all callers before changing this interface; a mismatch here will break cross-file behavior.",
    "caller_contract": "Preserve the {sym} interface and inspect its callers before changing return shape or semantics.",
    "sibling_match": "Sibling pattern nearby: {line}. Your implementation should match.",
}


def translate_to_action(evidence_block: str, phase: Phase) -> str:
    if phase in (Phase.ORIENT, Phase.VIEW):
        return evidence_block
    lines: list[str] = []
    for line in evidence_block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "[WITNESS]" in stripped and "->" in stripped:
            m = re.search(r"\[WITNESS\]\s+(\S+)\s+\w+\s+by\s+->\s+([^`]+)", stripped)
            if m:
                lines.append(ACTION_TEMPLATES["witness_call"].format(
                    sym=m.group(1), loc=m.group(2).strip()))
                continue
        if "[CALLERS]" in stripped:
            m = re.search(
                r"\[CALLERS\]\s+([^:]+):\s+\d+\s+verified caller\(s\).+preserve this interface",
                stripped,
            )
            if m:
                lines.append(ACTION_TEMPLATES["caller_contract"].format(
                    sym=m.group(1).strip()))
                continue
            lines.append(ACTION_TEMPLATES["caller_check"])
            continue
        if "[SIBLINGS]" in stripped:
            lines.append(ACTION_TEMPLATES["sibling_match"].format(line=stripped))
            continue
        lines.append(stripped)
    return "\n".join(lines)
