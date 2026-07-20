"""D-10 (run6 audit): obligation/requirement text must truncate at a WORD
boundary, never mid-character — the '33'-class dumb-character-logic bug.

Live kill: a hard text[:160] slice severed requirements mid-word ("unimodal d…",
"first g…", "take xy as single parameter…"), cutting the exact discriminating
clause the model needed to disambiguate the fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from groundtruth.runtime.obligations import _truncate_at_word  # noqa: E402


def _is_word_clean(original: str, out: str, limit: int) -> bool:
    if len(out) > limit:
        return False
    if not out.endswith("…"):
        return out == original  # unchanged when it fit
    kept = out[:-1]
    nxt = original[len(kept):len(kept) + 1]
    # boundary char after the cut, OR the whole budget was one long token
    return nxt in ("", " ", "\t", "\n") or " " not in original[:limit]


def test_never_cuts_mid_word():
    cases = [
        ("The points should follow a unimodal distribution across the region", 40),
        ("take xy as single parameter (possibly) not separate ones", 35),
        ("Add support for PEP 735 dependency groups with include-group resolution", 50),
    ]
    for text, lim in cases:
        out = _truncate_at_word(text, lim)
        assert _is_word_clean(text, out, lim), f"mid-word cut: {out!r} (lim {lim})"
        assert out.endswith("…"), "a truncated string must mark the cut"


def test_short_text_unchanged():
    assert _truncate_at_word("short", 60) == "short"


def test_bound_never_exceeded():
    long = "word " * 100
    for lim in (10, 33, 80, 160):
        assert len(_truncate_at_word(long, lim)) <= lim


def test_single_long_token_falls_back_to_hard_cut():
    tok = "x" * 200
    out = _truncate_at_word(tok, 30)
    assert len(out) == 30 and out.endswith("…")
