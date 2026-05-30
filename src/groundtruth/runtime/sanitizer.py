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


# ---------------------------------------------------------------------------
# Cluster-1: semantic validators + boundary Safe Renderer (B1/B2/B3/B3b).
#
# clip_balanced/is_well_formed_clause above are STRUCTURAL only: they pass
# `raises raise,exc_info[1].with_traceback` (brackets balanced) and an empty
# guard. The validators below add SEMANTIC checks; sanitize_evidence_block is
# the single boundary every append/prepend/brief emission routes through.
# Language-agnostic by design (GT is multi-language) — no Python `ast`: an
# exception spec is a dotted-identifier list, not a parsed AST node. Research
# basis: structurally-balanced-but-wrong context degrades agents (The
# Distracting Effect, arXiv 2505.06914) -> suppress, don't render.
# ---------------------------------------------------------------------------

# statement keywords across common languages that can never be an exception NAME
_STMT_KEYWORDS = frozenset({
    "raise", "return", "throw", "throws", "yield", "if", "else", "elif", "for",
    "while", "try", "except", "catch", "finally", "with", "def", "fn", "func",
    "class", "struct", "import", "from", "pass", "break", "continue", "and",
    "or", "not", "in", "is", "lambda", "async", "await", "del", "global",
    "nonlocal", "assert", "match", "case", "new", "panic", "defer", "go",
})
# a single exception name: dotted identifier starting with a letter/underscore
_EXC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
# a `[MARKER` opener cut before its closing `]`, left at the end of a clip
_PARTIAL_MARKER_RE = re.compile(r"\[[A-Z][A-Z _]*$")


def valid_exception_spec(s: str) -> bool:
    """True iff ``s`` is a comma-separated list of valid exception NAMES — a
    dotted identifier each, none a statement keyword, none containing call/
    subscript/operator syntax. Rejects the verified nonsense
    ``raise,exc_info[1].with_traceback``; accepts ``TypeError,ValueError``,
    ``ConanException``, ``pkg.mod.MyError``. Language-agnostic."""
    s = s.strip()
    if not s:
        return False
    parts = [p.strip() for p in s.split(",")]
    if any(not p for p in parts):
        return False
    for p in parts:
        if not _EXC_NAME_RE.match(p):
            return False
        if any(seg in _STMT_KEYWORDS for seg in p.split(".")):
            return False
    return True


def valid_guard_clause(s: str) -> bool:
    """True iff ``s`` is a non-empty, structurally well-formed conditional
    expression (not an empty/placeholder field, not a lone keyword, not a
    dangling/unbalanced fragment). Rejects the verified empty ``guard_clause:``."""
    s = s.strip()
    if not s or s in _STMT_KEYWORDS:
        return False
    return is_well_formed_clause(s)


def valid_return_shape(s: str) -> bool:
    """True iff ``s`` is a non-empty return shape. GT renders returns as
    ``<label>|<expr>`` or a bare ``<expr>``; the expression part must be
    non-empty and structurally well-formed."""
    s = s.strip()
    if not s:
        return False
    rhs = s.split("|", 1)[1].strip() if "|" in s else s
    if not rhs:
        return False
    return is_well_formed_clause(rhs)


# split a `Contract:` body on the ` | ` segment separator (NOT the spaceless
# `value|expr` pipe that lives inside a single segment)
_SEGMENT_SPLIT_RE = re.compile(r"\s\|\s")
_CONTRACT_RE = re.compile(r"^(\s*)Contract:\s*(.*)$")
_PRESERVE_RE = re.compile(r"^(\s*)(?:Preserve|PRESERVE):\s*(.*)$")
_KV_RE = re.compile(r"^(\w+):\s*(.*)$")


def _clean_contract_line(line: str):
    """Return a cleaned `Contract:` line, or None to suppress it. Drops invalid
    ``raises``/``returns`` segments (semantic), keeps valid ones and
    ``preserve``/other segments unchanged."""
    m = _CONTRACT_RE.match(line)
    if not m:
        return line
    indent, body = m.group(1), m.group(2)
    kept = []
    for seg in _SEGMENT_SPLIT_RE.split(body):
        seg = seg.strip()
        if not seg:
            continue
        low = seg.lower()
        if low == "raises" or low.startswith("raises "):
            spec = seg[len("raises"):].strip()  # Gap-1: bare `raises` -> spec="" -> drop
            if spec and valid_exception_spec(spec):
                kept.append(seg)
        elif low == "returns" or low.startswith("returns "):
            shape = seg[len("returns"):].strip()  # Gap-1: bare `returns` -> drop
            if shape and valid_return_shape(shape):
                kept.append(seg)
        else:
            kept.append(seg)  # preserve/other: not in the verified defect scope
    if not kept:
        return None
    return f"{indent}Contract: " + " | ".join(kept)


def _clean_preserve_line(line: str):
    """Return the `Preserve:` line, or None to suppress an empty/invalid field
    (the verified empty ``guard_clause:``)."""
    m = _PRESERVE_RE.match(line)
    if not m:
        return line
    kv = _KV_RE.match(m.group(2))
    kind, value = (kv.group(1), kv.group(2)) if kv else (None, m.group(2))
    if not value.strip():
        return None
    if kind == "guard_clause" and not valid_guard_clause(value):
        return None
    return line


def _cap_at_line_boundary(block: str, max_chars: int) -> str:
    """Cap ``block`` at the last complete LINE that fits, append an explicit
    ``…``. Never a raw byte slice. If the first line alone exceeds the budget,
    clause-safe-clip it via clip_balanced (still never mid-token)."""
    lines = block.split("\n")
    out, n = [], 0
    for ln in lines:
        add = len(ln) + (1 if out else 0)
        if n + add > max_chars:
            break
        out.append(ln)
        n += add
    if not out:
        first = clip_balanced(lines[0], max_chars)
        return (first + "\n…") if first else ""
    capped = "\n".join(out)
    if len(block) > len(capped):
        capped += "\n…"
    return capped


def sanitize_evidence_block(text: str, max_chars: int | None = None) -> str:
    """Single boundary Safe Renderer for every agent-facing GT emission.

    - drops hidden ``[GT_*]`` diagnostics;
    - suppresses malformed exception specs / empty or invalid contract fields
      (semantic, beyond clip_balanced's structural check);
    - drops an orphaned ``[GT KEY CONTRACTS]`` header left with no valid field;
    - never raw-slices: caps only at a line boundary with an explicit ``…``;
    - strips a trailing partial ``[MARKER`` opener (no truncated marker names).
    Returns "" when nothing survives (correct-or-quiet)."""
    if not text or not text.strip():
        return ""
    cleaned = []
    for raw in text.split("\n"):
        if is_hidden_line(raw):
            continue
        line = _clean_contract_line(raw)
        if line is None:
            continue
        line = _clean_preserve_line(line)
        if line is None:
            continue
        cleaned.append(line)
    # drop an orphaned [GT KEY CONTRACTS] header (no Preserve line follows)
    pruned = []
    for i, line in enumerate(cleaned):
        if line.strip() == "[GT KEY CONTRACTS]":
            nxt = cleaned[i + 1].strip() if i + 1 < len(cleaned) else ""
            if not (nxt.startswith("Preserve:") or nxt.startswith("PRESERVE:")):
                continue
        pruned.append(line)
    block = _PARTIAL_MARKER_RE.sub("", "\n".join(pruned)).rstrip()
    if max_chars is not None and len(block) > max_chars:
        block = _cap_at_line_boundary(block, max_chars)
    return block


def join_without_glue(left: str, right: str) -> str:
    """Concatenate two agent-facing fragments with a guaranteed newline boundary
    so GT evidence never fuses onto file/shell content (the verified
    ``text wit# SPDX`` / ``[CATCHEHere's`` glue)."""
    if not left:
        return right
    if not right:
        return left
    if left.endswith("\n") or right.startswith("\n"):
        return left + right
    return left + "\n" + right
