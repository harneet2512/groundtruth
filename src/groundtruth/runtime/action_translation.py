"""Product-owned graph fact to action translation."""

from __future__ import annotations

import re

from .context_policy import Phase


ACTION_TEMPLATES = {
    "caller_risk": (
        "Changing {callee} risks breaking the caller at {loc}. Inspect before editing."
    ),
    "callee_contract": ("You call {callee} (defined at {loc}). Preserve its interface."),
    "caller_check": (
        "Check all callers before changing this interface — a mismatch breaks cross-file behavior."
    ),
    "caller_contract": (
        "Preserve the {sym} interface; its callers depend on the current signature."
    ),
    "sibling_match": ("Sibling pattern: {line}. Match this in your implementation."),
}

# D5 fix: [WITNESS] X called by -> Y means X is the CALLEE (the function in the
# viewed file), Y is where the CALLER lives.  Producer (gt_mini_patch.py) emits
# w["target"] as X for caller direction so the template binds the correct symbol.
_WITNESS_CALLED_BY_RE = re.compile(r"\[WITNESS\]\s+(\S+)\s+called\s+by\s+->\s+(.+)")
_WITNESS_CALLS_RE = re.compile(r"\[WITNESS\]\s+(\S+)\s+calls\s+->\s+(.+)")
_CALLERS_COUNT_RE = re.compile(r"\[CALLERS\]\s+([^:]+):\s+(\d+)\s+verified caller")


def translate_to_action(evidence_block: str, phase: Phase) -> str:
    if phase in (Phase.ORIENT, Phase.VIEW):
        return evidence_block
    lines: list[str] = []
    for line in evidence_block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Caller direction: "X called by -> file:line `snippet`"
        # D5: X is the callee, the location is where the CALLER lives.
        m = _WITNESS_CALLED_BY_RE.search(stripped)
        if m:
            lines.append(stripped)  # keep original fact
            lines.append(
                ACTION_TEMPLATES["caller_risk"].format(
                    callee=m.group(1), loc=m.group(2).strip().split("`")[0].strip()
                )
            )
            continue
        # Callee direction: "X calls -> file:line `snippet`"
        m = _WITNESS_CALLS_RE.search(stripped)
        if m:
            lines.append(stripped)
            lines.append(
                ACTION_TEMPLATES["callee_contract"].format(
                    callee=m.group(1), loc=m.group(2).strip().split("`")[0].strip()
                )
            )
            continue
        if "[CALLERS]" in stripped:
            lines.append(stripped)  # keep the caller list
            m = _CALLERS_COUNT_RE.search(stripped)
            if m:
                lines.append(ACTION_TEMPLATES["caller_contract"].format(sym=m.group(1).strip()))
            else:
                lines.append(ACTION_TEMPLATES["caller_check"])
            continue
        if "[SIBLINGS]" in stripped:
            lines.append(stripped)
            lines.append(ACTION_TEMPLATES["sibling_match"].format(line=stripped))
            continue
        lines.append(stripped)
    return "\n".join(lines)
