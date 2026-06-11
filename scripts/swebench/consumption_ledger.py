#!/usr/bin/env python3
"""GT block consumption ledger — delivered → used/enforced pairs from trajectories."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_BLOCK_PATTERNS: tuple[tuple[str, str], ...] = (
    ("l3b.evidence", r"<gt-evidence"),
    ("l3b.contract", r"<gt-contract"),
    ("brief", r"<gt-task-brief"),
    ("consensus", r"<gt-scope"),
    ("cochange", r"<gt-cochange"),
    ("nudge", r"<gt-nudge"),
    ("graph_map", r"\[WITNESS\]|\[CALLER\]|\[IMPACT\]"),
)

_FILE_RE = re.compile(r"(?:^|\s)([\w./-]+\.(?:py|go|rs|js|ts|tsx|jsx|java|rb))\b")


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _extract_blocks(observation: str) -> list[dict[str, str]]:
    obs = observation or ""
    blocks: list[dict[str, str]] = []
    seen_kinds: set[str] = set()
    content_hash = _content_hash(obs[:2000])
    for kind, pat in _BLOCK_PATTERNS:
        if kind in seen_kinds:
            continue
        if re.search(pat, obs, re.I):
            blocks.append({"kind": kind, "content_hash": content_hash})
            seen_kinds.add(kind)
    return blocks


def _action_files(action: str) -> set[str]:
    return {m.group(1) for m in _FILE_RE.finditer(action or "")}


def _token_overlap(a: str, b: str, min_len: int = 5) -> bool:
    tokens_a = {t.lower() for t in re.findall(r"[A-Za-z_][\w]{4,}", a or "")}
    tokens_b = {t.lower() for t in re.findall(r"[A-Za-z_][\w]{4,}", b or "")}
    return bool(tokens_a & tokens_b)


def build_consumption_ledger(
    trajectory: list[dict] | dict,
    *,
    window: int = 3,
) -> dict[str, Any]:
    """Scan trajectory steps for GT deliveries and follow-through in next ``window`` turns."""
    if isinstance(trajectory, dict):
        steps = trajectory.get("trajectory") or trajectory.get("steps") or []
    else:
        steps = trajectory
    if not isinstance(steps, list):
        steps = []

    entries: list[dict[str, Any]] = []
    delivered = consumed = enforced = 0

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        obs = str(step.get("observation") or step.get("output") or "")
        blocks = _extract_blocks(obs)
        if not blocks:
            continue
        turn = i + 1
        future_actions: list[str] = []
        for j in range(i + 1, min(i + 1 + window, len(steps))):
            act = str((steps[j] or {}).get("action") or "")
            if act:
                future_actions.append(act)
        future_text = "\n".join(future_actions)
        future_files: set[str] = set()
        for act in future_actions:
            future_files |= _action_files(act)

        for blk in blocks:
            delivered += 1
            is_consumed = _token_overlap(obs, future_text) or bool(future_files)
            is_enforced = any(
                "pytest" in a.lower() or "test" in a.lower() for a in future_actions
            )
            if is_consumed:
                consumed += 1
            if is_enforced:
                enforced += 1
            entries.append({
                "turn": turn,
                "kind": blk["kind"],
                "content_hash": blk["content_hash"],
                "consumed": is_consumed,
                "enforced": is_enforced,
                "window": window,
            })

    return {
        "schema": "gt.consumption_ledger.v1",
        "gt_blocks_delivered": delivered,
        "gt_blocks_consumed": consumed,
        "gt_blocks_enforced": enforced,
        "entries": entries,
    }


def ledger_from_trajectory_path(path: str, *, window: int = 3) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return build_consumption_ledger([], window=window)
    steps = data.get("trajectory") if isinstance(data, dict) else data
    return build_consumption_ledger(steps or [], window=window)
