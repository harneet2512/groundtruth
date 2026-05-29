"""Sanitize every agent-visible GT message.

Strips hidden diagnostic prefixes, enforces character caps,
and validates that only allowed markers reach the agent.
Shared between OH adapter and MCP product face.
"""
from __future__ import annotations

import re

_HIDDEN_PREFIXES = (
    "[GT_META]", "[GT_STATUS]", "[GT_CONFIG]", "[GT_TRACE]",
    "[GT_DELIVERY]", "[GT_COST]", "[GT_PAYLOAD]", "[GT_LLM_CONFIG]",
    # Brief-runner diagnostics — were stripped only by a local filter in the
    # wrapper brief path; centralized here so every strip site shares one
    # authority and they cannot re-leak through a path that doesn't know them.
    "[GT_RANK_DIAG]", "[GT_BRIEF_DIAG]",
)

# A trailing binary/word operator means the clause was cut mid-expression.
_TRAILING_OP_RE = re.compile(
    r"(?:\s+(?:and|or|not|in|is)\b"
    r"|\s*(?:->|\+|-|\*|/|%|<=|>=|==|!=|<|>|&&|\|\||&|\||\^|~|=|,))\s*$"
)


def is_hidden_line(line: str) -> bool:
    s = line.strip()
    return any(s.startswith(p) for p in _HIDDEN_PREFIXES)


def is_well_formed_clause(s: str) -> bool:
    """True if ``s`` is a balanced code/expression fragment safe to show an
    agent: quotes balanced, bracket depth returns to zero, not left inside a
    string literal, and not ending on a dangling binary operator. Operates on
    quotes/brackets only, so it is language-agnostic."""
    in_str = ""
    esc = False
    depth = 0
    for ch in s:
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = ""
            continue
        if ch in "\"'":
            in_str = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return False
    if in_str or depth != 0:
        return False
    return _TRAILING_OP_RE.search(s.strip()) is None


def clip_balanced(text: str, max_len: int | None = None) -> str:
    """Return the longest well-formed prefix of ``text`` (first clipped to
    ``max_len`` chars when given), or "" when no non-trivial well-formed prefix
    exists.

    Truncating arbitrary source text (a guard condition, a ``raise`` statement)
    at a fixed byte budget can split inside a string literal or a parenthesised
    expression, leaving the agent an unterminated literal
    (``raise TypeError("DocumentSplitter expects a List of Document``) or a line
    ending on a dangling operator (``... (documents and not``) — malformed
    content that violates correct-or-quiet. This walks back to the last position
    where quotes are balanced AND bracket depth is zero, drops a trailing partial
    identifier and any dangling binary operator, and is idempotent / safe on
    already-malformed input (so it repairs values stored by an older indexer
    build). Generalizes across languages (it reasons about quotes/brackets, not
    Python syntax)."""
    if not text:
        return ""
    text = text.rstrip()
    budget = len(text) if max_len is None else min(len(text), max_len)

    in_str = ""
    esc = False
    depth = 0
    safe = 0  # furthest prefix length that is balanced and outside any string
    for i, ch in enumerate(text):
        # boundary BEFORE consuming text[i]; record when reachable & balanced
        if i <= budget and not in_str and depth == 0:
            safe = i
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = ""
            continue
        if ch in "\"'":
            in_str = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
    # whole text balanced and within budget -> keep all of it
    if not in_str and depth == 0 and len(text) <= budget:
        safe = len(text)

    # never end mid-identifier (only when the cut fell inside a word)
    if 0 < safe < len(text):
        before = text[safe - 1]
        after = text[safe]
        if (before.isalnum() or before == "_") and (after.isalnum() or after == "_"):
            m = re.search(r"\w+$", text[:safe])
            if m:
                safe = m.start()

    prefix = text[:safe].rstrip()
    # strip any dangling trailing binary operator(s), repeatedly
    prev = None
    while prefix and prev != prefix:
        prev = prefix
        prefix = _TRAILING_OP_RE.sub("", prefix).rstrip()
    return prefix


def sanitize(text: str, *, max_chars: int = 2000) -> str:
    """Remove hidden lines and enforce character cap."""
    lines = [ln for ln in text.splitlines() if not is_hidden_line(ln)]
    cleaned = "\n".join(lines).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars - 3] + "..."
    return cleaned


def has_leak(text: str) -> bool:
    """True if text contains any hidden diagnostic prefix."""
    return any(p in text for p in _HIDDEN_PREFIXES)
