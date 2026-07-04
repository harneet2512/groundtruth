"""Edge-case coverage for scripts/swebench/measure_l3_engagement.py.

Anti-benchmaxxing: all fixtures here are synthetic and language-mixed (Python
identifiers chosen to mimic real auth/middleware code, but no Live-Lite-specific
task data is used).  Tests focus on the contract — tokenizer/stopword filter,
edit-completion rule, division-by-zero, malformed inputs, monotonic window
behaviour — not on per-task patterns.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# scripts/swebench is not on sys.path by default in pytest discovery.
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "swebench"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import measure_l3_engagement as mle  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "engagement" / "fixtures"


def _run(name: str, **kwargs):
    return mle.measure_l3_engagement(FIXTURES / name, **kwargs)


# --- core edge-case fixtures ------------------------------------------------


def test_zero_engagement():
    out = _run("zero_engagement.json")
    assert out["edit_count"] == 1
    assert out["edits_with_evidence"] == 1
    assert out["engagement_rate"] == 0.0
    assert out["avg_substring_match"] == 0.0
    [edit] = out["per_edit"]
    assert edit["evidence_present"] is True
    assert edit["substring_match"] == 0


def test_partial_engagement():
    out = _run("partial_engagement.json")
    assert out["edit_count"] == 1
    assert out["edits_with_evidence"] == 1
    [edit] = out["per_edit"]
    # Exactly one verbatim token from evidence ("verify_password") leaks into
    # the next-3 window via the agent's thought.
    assert edit["substring_match"] == 1
    # Evidence has many distinct tokens so match_rate should be small.
    assert edit["match_rate"] < 0.1, edit


def test_all_shell():
    out = _run("all_shell.json")
    assert out["edit_count"] == 0
    assert out["edits_with_evidence"] == 0
    # No division-by-zero — engagement_rate falls back to 0.0.
    assert out["engagement_rate"] == 0.0
    assert out["avg_match_rate"] == 0.0
    assert out["avg_substring_match"] == 0.0
    assert out["per_edit"] == []


def test_malformed_evidence():
    """gt_evidence as None / empty dict / missing state must not crash."""
    out = _run("malformed_evidence.json")
    # All three malformed edits are still counted as edits (verb-pass) but
    # never advance to evidence processing because evidence resolves to "".
    assert out["edit_count"] == 3
    assert out["edits_with_evidence"] == 0
    assert out["engagement_rate"] == 0.0
    assert len(out["per_edit"]) == 3
    for edit in out["per_edit"]:
        assert edit["evidence_present"] is False
        assert edit["match_rate"] == 0.0
        assert edit["substring_match"] == 0


def test_huge_evidence_perf_and_validity():
    """50KB evidence should still process in well under 1s."""
    t0 = time.perf_counter()
    out = _run("huge_evidence.json")
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"huge-evidence run took {elapsed:.3f}s"
    assert out["edit_count"] == 1
    assert out["edits_with_evidence"] == 1
    [edit] = out["per_edit"]
    assert edit["evidence_present"] is True
    # The window thought + observation seeds at least 1 verbatim hit.
    assert edit["substring_match"] >= 1
    # Token count must be a sensible bounded integer, not exploding.
    assert 100 < edit["evidence_token_count"] < 100_000


def test_no_next_steps():
    """Edit at the last index — i+1 doesn't exist, must be NaN-safe."""
    out = _run("no_next_steps.json")
    assert out["edit_count"] == 1
    assert out["edits_with_evidence"] == 1
    [edit] = out["per_edit"]
    assert edit["evidence_present"] is True
    # Empty window → no overlap, no substring matches.
    assert edit["window_token_count"] == 0
    assert edit["match_rate"] == 0.0
    assert edit["substring_match"] == 0
    assert out["engagement_rate"] == 0.0


def test_multiple_edits():
    out = _run("multiple_edits.json")
    assert out["edit_count"] == 3
    assert out["edits_with_evidence"] == 3
    assert len(out["per_edit"]) == 3
    indices = [e["step"] for e in out["per_edit"]]
    assert indices == sorted(indices)
    assert len(set(indices)) == 3
    # All three verbs should be honoured (create, str_replace, insert).
    verbs = [e["action_verb"] for e in out["per_edit"]]
    assert set(verbs) == {"create", "str_replace", "insert"}


# --- contract probes --------------------------------------------------------


def test_window_param_monotonicity():
    """Larger window must never *decrease* the substring/match-rate signal
    on the partial-engagement fixture (more next-steps = more chances to
    hit evidence tokens)."""
    rates: dict[int, float] = {}
    subs: dict[int, float] = {}
    for w in (1, 2, 3, 5):
        out = _run("partial_engagement.json", window=w)
        rates[w] = out["avg_match_rate"]
        subs[w] = out["avg_substring_match"]
    for prev, nxt in zip((1, 2, 3), (2, 3, 5)):
        assert rates[nxt] >= rates[prev], (rates, prev, nxt)
        assert subs[nxt] >= subs[prev], (subs, prev, nxt)
    # Sanity: zero-engagement fixture stays zero across all windows.
    for w in (1, 2, 3, 5):
        out = _run("zero_engagement.json", window=w)
        assert out["engagement_rate"] == 0.0


def test_tokenizer_stopword_filter():
    """def / class / func / pub / let / var must be stripped at tokenize-time."""
    text = "def class func function fn let var const pub static public private"
    tokens = mle.tokenize(text)
    for stop in ("def", "class", "func", "function", "let", "var", "pub"):
        assert stop not in tokens, (stop, tokens)


def test_tokenizer_short_token_filter():
    """Tokens shorter than 3 characters must be dropped (regex requires >=3)."""
    tokens = mle.tokenize("a bb ccc dddd")
    assert "a" not in tokens
    assert "bb" not in tokens
    assert "ccc" in tokens
    assert "dddd" in tokens


def test_tokenizer_splits_qualified_names():
    """SPLIT_RE must break dotted/slashed/backslashed/colon paths."""
    tokens = mle.tokenize("src/groundtruth/auth.login::verify_password\\token")
    assert "groundtruth" in tokens
    assert "auth" in tokens
    assert "login" in tokens
    assert "verify_password" in tokens
    assert "token" in tokens  # split out from `\token` segment


def test_existing_language_fixtures_smoke():
    """Pre-existing per-language fixtures must still parse cleanly."""
    for name in ("go_sample.json", "rust_sample.json", "js_sample.json", "java_sample.json"):
        path = FIXTURES / name
        if not path.exists():
            pytest.skip(f"{name} not present")
        out = mle.measure_l3_engagement(path)
        assert out["edit_count"] >= 1
        assert isinstance(out["per_edit"], list)


def test_action_verb_filter_rejects_view():
    """`str_replace_editor view ...` must NOT be classified as an edit."""
    traj = {
        "trajectory": [
            {
                "action": "str_replace_editor view /testbed/foo.py",
                "state": {"gt_evidence": "x" * 200},
            }
        ]
    }
    tmp = FIXTURES / "_tmp_view_only.json"
    tmp.write_text(json.dumps(traj), encoding="utf-8")
    try:
        out = mle.measure_l3_engagement(tmp)
        assert out["edit_count"] == 0
        assert out["per_edit"] == []
    finally:
        tmp.unlink(missing_ok=True)


def test_short_evidence_skipped():
    """len(evidence) <= 100 must not be promoted to evidence_present=True."""
    traj = {
        "trajectory": [
            {
                "action": "str_replace_editor str_replace /testbed/foo.py --old_str a --new_str b",
                "state": {"gt_evidence": "[CALLER] tiny"},
            },
            {"action": "execute_bash", "observation": "ls"},
        ]
    }
    tmp = FIXTURES / "_tmp_short_ev.json"
    tmp.write_text(json.dumps(traj), encoding="utf-8")
    try:
        out = mle.measure_l3_engagement(tmp)
        assert out["edit_count"] == 1
        assert out["edits_with_evidence"] == 0
        [edit] = out["per_edit"]
        assert edit["evidence_present"] is False
    finally:
        tmp.unlink(missing_ok=True)
