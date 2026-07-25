"""FEATURE #15 GT_LOC_RESLOT (+ #4 localization) — the producer latch pre-empted WS-2 FLARE.

WS-2 FLARE (`_ss_flare_redeliver`) exists to un-suppress a still-relevant localization target the
moment the agent is in DEMONSTRATED difficulty (a degenerate loop fired this episode) — the fact
scrolled out of the effective window, or the agent lost the thread.

But FLARE runs at the SS-REFEREE layer, downstream of production, while `_loc_reslot_payload` spends
its own once-per-attempt latch AT PRODUCTION. So after the first delivery the payload returned ""
immediately and FLARE had nothing to un-suppress: two mechanisms designed to cooperate, with the
latch silently pre-empting the one that matters most — precisely when the agent is stuck.

The latch is now FLARE-aware: re-production is permitted ONLY under the conditions FLARE itself
requires (GT_SS_FLARE on AND a loop detected), so it is byte-identical whenever that flag is off,
and it does NOT add a dose — the re-produced answer re-competes for the same <=1 slot.
"""
from __future__ import annotations
import re
import os

_SRC = os.path.join(os.path.dirname(__file__), "..", "gt_mini_patch.py")


def _body(fn: str, chars: int = 2200) -> str:
    src = open(os.path.abspath(_SRC), encoding="utf-8").read()
    i = src.index(f"def {fn}(")
    return src[i:i + chars]


def test_latch_consults_flare_before_refusing():
    """The regression: an unconditional `or _loc_reslot_delivered` early-return."""
    b = _body("_loc_reslot_payload")
    assert "_ss_flare_redeliver" in b, \
        "the reslot latch does not consult FLARE — a stuck agent can never get the target again"
    assert not re.search(r"if\s+_GT_BASELINE\s+or\s+_loc_reslot_delivered\s+or", b), \
        "the latch still short-circuits before FLARE is considered"


def test_flare_recheck_is_gated_on_the_localization_class():
    """FLARE is localization-ONLY by doctrine (ride the agent's thread, never re-point it)."""
    b = _body("_loc_reslot_payload")
    assert 'is_loc=True' in b and 'post_search.localize' in b


def test_flare_itself_requires_flag_and_a_real_loop():
    """Byte-identical when GT_SS_FLARE is off: FLARE must gate on the flag AND a detected loop,
    so the re-production path cannot open on its own."""
    f = _body("_ss_flare_redeliver", 1400)
    assert '_ss_enabled("GT_SS_FLARE")' in f, "FLARE is not flag-gated"
    assert "_detect_loop_fired" in f, "FLARE does not require demonstrated agent difficulty"


def test_latch_still_spends_on_delivery():
    """Re-production must remain the EXCEPTION — the latch is still spent at production."""
    b = _body("_loc_reslot_payload", 5000)
    assert "_loc_reslot_delivered = True" in b, \
        "the once-per-attempt latch must still be spent, or localization would re-fire every search"
