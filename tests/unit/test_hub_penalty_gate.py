"""Red-before-green: the hub penalty must bite well-evidenced hubs.

Grounded in a real-graph observation (canary repro on GT's own package, 2026-05-30):
`hooks/post_edit.py` had hub_pen=0.984 and evidence ~0.965 but received hub_sub=0.0
because the gate `if evidence_pre_hub < w_hub` (v7_4_brief.py:265) zeroed the penalty
for exactly the well-evidenced hubs that out-rank specific modules (the B4 failure).
59.3% of hub candidates were silently un-penalized on the real graph.

Product requirement (DOC_OF_HONOR Layer 0.5 — graph-theory degree normalization):
a hub and a non-hub with IDENTICAL evidence must NOT tie; the hub ranks lower. And
a non-hub (hub_pen==0) must be a strict no-op (the no-regression property).
"""
from groundtruth.pretask.v7_4_brief import (
    DEFAULT_WEIGHTS,
    _ablation_weights,
    _total_score,
)

W = _ablation_weights("C", dict(DEFAULT_WEIGHTS))


def _comp(hub_pen: float, lex: float = 0.9, path: float = 0.9) -> dict:
    return {
        "sem": 0.0, "lex": lex, "path": path, "reach": 0.0,
        "anchor_prox": 0.0, "commit": 0.0, "hub_pen": hub_pen, "frame": 0.0,
    }


def test_hub_with_equal_evidence_ranks_below_nonhub():
    """A well-evidenced hub must score strictly below a non-hub with identical
    evidence. RED on the dead gate (both got hub_sub=0 -> exact tie); GREEN after."""
    hub = _total_score(_comp(hub_pen=0.9), W)
    nonhub = _total_score(_comp(hub_pen=0.0), W)
    assert hub < nonhub, f"hub {hub} must rank below non-hub {nonhub} (penalty dead?)"


def test_penalty_scales_with_hubness():
    """Higher hub_pen -> strictly lower score for equal evidence (monotonic).
    RED on the dead gate (both evidence >> w_hub -> both un-penalized -> tie)."""
    s_low = _total_score(_comp(hub_pen=0.2), W)
    s_high = _total_score(_comp(hub_pen=0.9), W)
    assert s_high < s_low, f"hub_pen 0.9 ({s_high}) must score below hub_pen 0.2 ({s_low})"


def test_nonhub_is_strict_noop():
    """hub_pen==0 -> penalty term is 0 -> score == raw evidence (no-regression).
    This invariant must hold on BOTH the gated and the fixed code."""
    c = _comp(hub_pen=0.0)
    raw = W["W_LEX"] * c["lex"] + W["W_PATH"] * c["path"]
    assert abs(_total_score(c, W) - raw) < 1e-9


def test_high_evidence_hub_not_overpenalized():
    """W_HUB=0.1 is a tie-breaker, not a sledgehammer: a hub whose evidence beats
    a rival by more than w_hub still wins (legitimately-relevant hub gold, e.g.
    post_edit on a post_edit issue, stays top). Holds on gated AND fixed code."""
    strong_hub = _total_score(_comp(hub_pen=0.98, lex=0.95, path=0.95), W)
    weak_specific = _total_score(_comp(hub_pen=0.0, lex=0.3, path=0.3), W)
    assert strong_hub > weak_specific


def test_hub_wins_when_evidence_gap_exceeds_penalty():
    """The GUARANTEED invariant (max penalty = w_hub*hub_pen <= w_hub): a hub
    whose raw evidence beats a rival by MORE than w_hub still wins. gap 0.285 > 0.10."""
    hub = _total_score(_comp(hub_pen=1.0, lex=0.9, path=0.9), W)   # evid 0.855 - 0.10 = 0.755
    rival = _total_score(_comp(hub_pen=0.0, lex=0.6, path=0.6), W)  # evid 0.57
    assert hub > rival


def test_close_contest_hub_loses_BY_DESIGN():
    """The un-gating is a TIE-BREAKER: in a CLOSE contest (raw-evidence gap <
    w_hub*hub_pen) a hub now loses to a specific non-hub. This is INTENDED (the
    code comment: 'a tie-breaker that flips close hub-vs-specific contests'). This
    is the regime test_high_evidence_hub_not_overpenalized never exercised — it
    documents the real flip so the no-regression claim isn't over-read."""
    hub = _total_score(_comp(hub_pen=0.9, lex=0.9, path=0.9), W)      # evid 0.855 - 0.09 = 0.765
    specific = _total_score(_comp(hub_pen=0.0, lex=0.84, path=0.84), W)  # evid 0.798 (gap 0.057 < 0.09)
    assert specific > hub, "close contest: the specific non-hub wins (by design)"
