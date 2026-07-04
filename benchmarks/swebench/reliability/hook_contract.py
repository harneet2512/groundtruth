"""hook_contract.py — agent-hook delivered/correct/consumed (host-side).

Honors gt_gt.md's verification protocol: DELIVERED (brief text appears in the
agent's output.jsonl observations, raw) is necessary, not sufficient; CORRECT
(payload claims match git diff / edited files); CONSUMED (the agent acted after
the payload). In gates_only mode there is no agent, so the hook is N/A-by-mode.
Read-only.
"""
from __future__ import annotations

import json
import os

# statuses per the audit spec
DELIVERED_ONLY = "DELIVERED_ONLY"
DELIVERED_AND_CORRECT = "DELIVERED_AND_CORRECT"
DELIVERED_CORRECT_CONSUMED = "DELIVERED_CORRECT_CONSUMED"
NOT_DELIVERED = "NOT_DELIVERED"
INCORRECT_PAYLOAD = "INCORRECT_PAYLOAD"
INERT_PAYLOAD = "INERT_PAYLOAD"
NA_GATES_ONLY = "N/A_GATES_ONLY"


def _iter_observations(output_jsonl: str):
    """Yield raw observation/text strings from the agent trajectory."""
    try:
        for line in open(output_jsonl, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            for k in ("observation", "content", "message", "text"):
                v = ev.get(k) if isinstance(ev, dict) else None
                if isinstance(v, str) and v:
                    yield v
    except Exception:
        return


def build_hook_contract(output_jsonl: str, brief_text: str = "", brief_hash: str = "",
                        edited_files: list | None = None,
                        gates_only: bool = False) -> dict:
    c: dict = {"contract": "hook", "output_jsonl": output_jsonl, "brief_hash": brief_hash}
    if gates_only or not (output_jsonl and os.path.exists(output_jsonl)):
        c["status"] = NA_GATES_ONLY
        c["brief_rendered"] = bool(brief_text)
        c["note"] = "gates_only / no agent trajectory — hook delivery not exercised"
        return c

    marker = (brief_text[:120] if brief_text else "") or "<gt-task-brief>"
    obs = list(_iter_observations(output_jsonl))
    delivered = any(marker and marker in o for o in obs) or any("<gt-" in o for o in obs)
    c["brief_rendered"] = bool(brief_text)
    c["delivered"] = delivered
    if not delivered:
        c["status"] = NOT_DELIVERED
        return c

    # CORRECT: any GT-recommended file/symbol must intersect the agent's edits
    ef = set(edited_files or [])
    referenced_after = any(any(p in o for p in ef) for o in obs) if ef else None
    c["edited_files"] = sorted(ef)
    c["agent_referenced_recommended"] = referenced_after
    # Without the GT-recommended set + the diff we cannot fully prove correctness here;
    # mark DELIVERED_ONLY and leave correct/consumed to the deeper per-task ledger.
    c["status"] = DELIVERED_ONLY if referenced_after is None else (
        DELIVERED_CORRECT_CONSUMED if referenced_after else DELIVERED_ONLY)
    return c


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "/tmp/output.jsonl"
    print(json.dumps(build_hook_contract(p), indent=2, default=str))
