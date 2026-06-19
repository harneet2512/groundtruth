"""TDD for the balance-aware clip that prevents truncated/unterminated contract
guard values from reaching the agent (C1 — correct-or-quiet).

The defect: guard text is blind byte-sliced (parser.go consequenceText[:60] /
condText[:120]; oh_gt_full_wrapper.py p['value'][:60]; v1r path uncapped over an
already-mangled stored value). A fixed byte budget lands mid-string-literal or
mid-expression, so the agent receives an UNTERMINATED literal
(``raise TypeError("DocumentSplitter expects a List of Document``) or a line
ending on a dangling binary operator (``... (documents and not``).

`sanitizer.clip_balanced` must return the longest prefix that is well-formed
(balanced quotes/brackets, no trailing binary operator, no mid-identifier cut),
or "" when no non-trivial well-formed prefix exists. The test's balance oracle
is INDEPENDENT of the implementation so it validates real output, not itself.
"""
from __future__ import annotations

import re

from groundtruth.runtime.sanitizer import clip_balanced, is_well_formed_clause

# The two real malformed values that reached the agent (canary 26657916167 / 26651360055).
_REAL_CONSEQUENCE = 'raise TypeError("DocumentSplitter expects a List of Documents as input.")'
_REAL_COND = "not isinstance(documents, list) or (documents and not isinstance(documents[0], Document))"
_REAL_STORED = "raise: " + _REAL_COND + " -> " + _REAL_CONSEQUENCE


def _balanced(s: str) -> bool:
    """Independent oracle: quotes balanced, bracket depth returns to 0, not
    inside a string, not ending on a dangling binary operator. NOT the
    implementation under test."""
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
    if re.search(r"(\b(and|or|not|in|is)\b|[-+*/%<>=&|^~]|->)\s*$", s.strip()):
        return False
    return True


# ---- negative control: prove the test detects the actual bug class ----

def test_negative_control_blind_cut_is_malformed():
    """The OLD blind byte-slice produces an UNBALANCED value. If this ever
    passes the oracle, the oracle is too weak and the green test below is
    meaningless."""
    blind_consequence = _REAL_CONSEQUENCE[:60]
    assert blind_consequence == 'raise TypeError("DocumentSplitter expects a List of Document'
    assert not _balanced(blind_consequence), "blind [:60] must be detected as unbalanced (open quote+paren)"

    blind_cond = (_REAL_COND)[:53]  # lands at '(documents and not'
    assert blind_cond.endswith("and not") or blind_cond.endswith("and no")
    assert not _balanced(blind_cond), "blind cut of the cond must be unbalanced (open paren / trailing op)"


# ---- green: clip_balanced repairs already-malformed stored values ----

def test_repairs_open_string_literal():
    """An already-truncated open-quote value (from an old indexer build) is
    repaired to a balanced prefix, never emitted as an unterminated literal."""
    out = clip_balanced('raise TypeError("DocumentSplitter expects a List of Document')
    assert _balanced(out), f"clip_balanced left malformed output: {out!r}"
    assert '"' not in out or out.count('"') % 2 == 0
    assert out  # non-empty: 'raise TypeError' is a useful balanced prefix
    assert out.startswith("raise TypeError")


def test_repairs_trailing_operator_and_open_paren():
    out = clip_balanced("raise: not isinstance(documents, list) or (documents and not")
    assert _balanced(out), f"unbalanced: {out!r}"
    assert not re.search(r"\b(and|or|not)\s*$", out), f"dangling operator: {out!r}"
    assert "isinstance(documents, list)" in out


def test_wellformed_under_budget_unchanged():
    good = "raises ValueError,TypeError"
    assert clip_balanced(good) == good
    assert clip_balanced(good, 200) == good


def test_max_len_does_not_cut_mid_identifier():
    out = clip_balanced("returns Optional[User]", max_len=13)  # blind would give 'returns Optio'
    assert _balanced(out)
    assert not out.endswith("Optio")
    assert out in ("returns", "returns Optional[User]")  # word boundary, never mid-word


def test_max_len_does_not_cut_mid_string():
    out = clip_balanced(_REAL_CONSEQUENCE, max_len=60)
    assert _balanced(out), f"unbalanced: {out!r}"
    # either the full balanced literal (<=60 impossible here) or a safe prefix
    assert out.count('"') % 2 == 0


def test_drops_when_no_safe_prefix():
    # opens a string immediately, no balanced non-trivial prefix
    out = clip_balanced('"unterminated from char zero')
    assert out == "" or _balanced(out)


def test_idempotent():
    once = clip_balanced(_REAL_STORED, max_len=60)
    twice = clip_balanced(once, max_len=60)
    assert once == twice
    assert _balanced(once)


def test_is_well_formed_clause_matches_oracle():
    assert is_well_formed_clause("raises ValueError")
    assert not is_well_formed_clause('raise TypeError("open')
    assert not is_well_formed_clause("a or")
    assert not is_well_formed_clause("foo(bar")
