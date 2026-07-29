"""The payload anchor must match CONTEXT DEPTH, not take the last text repeat.

THE DEFECT (ARCH-C timing audit, 2026-07-29, telegram-bot-4673): the anchor text
("action was not executed") repeats 97x in the trajectory, and
``_payload_context_anchor`` took ``matches[-1]`` — inflating delivery_step from
~22 (true, proven by the payload's own 56-message length and mid-run staging
timestamp) to 148, and flipping 3 AHEAD directions to false BEHIND.

A payload is the EXACT conversation the model held at the carrying call, so the
true boundary is the match whose PRECEDING trajectory messages also equal the
payload's preceding messages — the deepest suffix match. Later repeats of the
same context text sit on top of DIFFERENT (later) conversation prefixes and die
on depth. Ties keep the LATEST candidate: never over-credit GT with an earlier
delivery than provable.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "scripts" / "swebench"), str(_REPO / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from consumption_ledger import _payload_context_anchor  # noqa: E402


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": text}


def test_repeated_context_text_anchors_at_the_deep_suffix_match() -> None:
    """Trajectory repeats the withhold marker 3x; only the FIRST occurrence sits on
    the payload's actual prefix. matches[-1] returns index 7 (wrong); suffix-aware
    anchoring returns index 3."""
    marker = "<exception>action was not executed</exception>"
    trajectory = [
        _msg("user", "Fix the bug."),                      # 0
        _msg("assistant", "cat src/a.py"),                 # 1
        _msg("user", "...contents of a..."),               # 2
        _msg("user", marker),                              # 3  <- TRUE boundary
        _msg("assistant", "sed -i 's/x/y/' src/a.py"),     # 4
        _msg("user", marker),                              # 5  repeat, different prefix
        _msg("assistant", "echo hello"),                   # 6
        _msg("user", marker),                              # 7  repeat, different prefix
    ]
    payload_messages = trajectory[:4] + [_msg("user", "GT CAPSULE BYTES")]
    delivery = {"message_index": 4, "payload_messages": payload_messages}
    index, reason = _payload_context_anchor(delivery, trajectory)
    assert reason is None
    assert index == 3, (
        f"anchored at {index}: the last text repeat is not the delivery boundary"
    )


def test_unique_context_still_anchors() -> None:
    trajectory = [
        _msg("user", "Fix it."),
        _msg("assistant", "grep -rn foo src/"),
        _msg("user", "src/foo.py:1: foo"),
    ]
    payload_messages = trajectory[:3] + [_msg("user", "CAPSULE")]
    delivery = {"message_index": 3, "payload_messages": payload_messages}
    index, reason = _payload_context_anchor(delivery, trajectory)
    assert (index, reason) == (2, None)


def test_true_tie_keeps_the_latest_candidate() -> None:
    """Two positions with IDENTICAL depth (a genuinely ambiguous trajectory) must
    keep the conservative latest anchor — never over-credit an earlier delivery."""
    marker = "ok"
    a, b = _msg("assistant", "echo hi"), _msg("user", marker)
    trajectory = [a, b, a, b]
    payload_messages = [a, b, _msg("user", "CAPSULE")]
    delivery = {"message_index": 2, "payload_messages": payload_messages}
    index, reason = _payload_context_anchor(delivery, trajectory)
    assert reason is None
    assert index == 3, "ambiguous depth must fall back to the latest match"
