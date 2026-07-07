"""Pin: SPEC §13.12/§14.5 — the write→test→fix retry ported to OH's terminal-finish topology.

DeepSWE owns the loop (post-submit retry). OH sets state=FINISHED before the wrapper sees the
finish (post-finish append = dead write), so the port runs the repo-native suite at the
edit→review transition (pre-finish) and feeds a REAL failure back via the arm-neutral
<test-feedback> tag. This pins the load-bearing properties:
  - byte-identical-off: flag unset -> _retry_count()==0 -> the container suite is NEVER run;
  - correct-or-quiet classify: pass(rc0) / no-runner(97) / no-root(96) / env-unverifiable /
    lost-sentinel(rc<0) all stay silent; only a REAL failure delivers;
  - fire-gate: only at a review transition (>=3 since last edit), one verify per NEW edit,
    capped at gt_agent._RETRY_MAX;
  - leak/arm posture: the delivered tag is <test-feedback> (NOT <gt-*>); the GT `gate_note`
    reminder is present only when NOT GT_BASELINE (arm-neutral feedback, GT-differential note).

The rc is captured by a subshell sentinel (__GT_TEST_RC__=$?), simulated here in the canned
container output so the classify path is exercised without running pytest for real.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_WRAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "swebench"))
sys.path.insert(0, _WRAP_DIR)
for _mod in ("litellm", "cost_tracking"):
    sys.modules.setdefault(_mod, SimpleNamespace(
        model_cost={}, success_callback=[], completion=lambda *a, **k: None,
        acompletion=None, completion_cost=lambda *a, **k: 0.0,
        track_cost=lambda *a, **k: None, CostTracker=object))
try:
    import oh_gt_full_wrapper as _w
except Exception:  # heavy sibling deps unavailable
    _w = None

skip = pytest.mark.skipif(_w is None, reason="oh_gt_full_wrapper import unavailable")


def _cfg(*, edits=(5,), action_count=10, verified=-1, used=0):
    return SimpleNamespace(
        _source_edit_actions=list(edits), action_count=action_count,
        _retry_last_verified_edit=verified, _retry_attempts_used=used)


def _runner(output):
    """A mock orig_run_action: ignores the command, returns a canned container response
    (test output + the subshell rc sentinel). Records that it was invoked."""
    calls = []

    def run(action):
        calls.append(action)
        return SimpleNamespace(content=output)
    run.calls = calls
    return run


def _delivered(obs):
    return getattr(obs, "content", "") or ""


@skip
def test_flag_off_never_runs_the_suite(monkeypatch):
    monkeypatch.delenv("GT_SELF_VERIFY_ATTEMPTS", raising=False)
    monkeypatch.delenv("GT_RETRY_ON_VERIFIER_FAIL", raising=False)
    run = _runner("FAILED x\n__GT_TEST_RC__=1")
    obs = SimpleNamespace(content="orig")
    out = _w._maybe_run_presubmit_tests(_cfg(), obs, run)
    assert run.calls == [], "flag OFF must never exec the container suite (byte-identical)"
    assert _delivered(out) == "orig"


@skip
@pytest.mark.parametrize("output,tag", [
    ("=== 5 passed ===\n__GT_TEST_RC__=0", "pass"),
    ("__GT_TEST_RC__=97", "no_runner"),
    ("__GT_TEST_RC__=96", "no_root"),
    ("bash: pytest: command not found\n__GT_TEST_RC__=127", "env_unverifiable"),
    ("=== 5 passed ===", "lost_sentinel"),   # no sentinel -> rc=-1 -> quiet
])
def test_correct_or_quiet_classifications(monkeypatch, output, tag):
    monkeypatch.setenv("GT_SELF_VERIFY_ATTEMPTS", "2")
    run = _runner(output)
    obs = SimpleNamespace(content="orig")
    out = _w._maybe_run_presubmit_tests(_cfg(), obs, run)
    assert run.calls, f"{tag}: the suite should have RUN (review transition reached)"
    assert _delivered(out) == "orig", f"{tag}: a non-failure must NOT deliver <test-feedback>"


@skip
def test_real_failure_delivers_arm_neutral_feedback(monkeypatch):
    monkeypatch.setenv("GT_SELF_VERIFY_ATTEMPTS", "2")
    monkeypatch.setattr(_w, "_GT_BASELINE", False)
    run = _runner("FAILED tests/test_x.py::t - AssertionError\n__GT_TEST_RC__=1")
    obs = SimpleNamespace(content="orig")
    out = _w._maybe_run_presubmit_tests(_cfg(), obs, run)
    body = _delivered(out)
    assert "<test-feedback" in body and "Tests failed" in body
    assert 'failure="assertion_error"' in body            # reused gt_agent classifier
    assert "GT pre_submit_intervention" in body           # GT-differential gate_note (not baseline)
    assert "__GT_TEST_RC__" not in body                   # the rc sentinel is stripped from the dose


@skip
def test_baseline_gets_feedback_without_the_gt_note(monkeypatch):
    monkeypatch.setenv("GT_SELF_VERIFY_ATTEMPTS", "2")
    monkeypatch.setattr(_w, "_GT_BASELINE", True)
    run = _runner("FAILED tests/test_x.py::t - AssertionError\n__GT_TEST_RC__=1")
    out = _w._maybe_run_presubmit_tests(_cfg(), SimpleNamespace(content="orig"), run)
    body = _delivered(out)
    assert "<test-feedback" in body                        # arm-neutral: feedback runs in baseline too
    assert "GT pre_submit_intervention" not in body        # but the GT reminder does NOT


@skip
@pytest.mark.parametrize("edits,ac,tag", [((), 10, "no_edit"), ((8,), 10, "too_soon")])
def test_fire_gate_holds(monkeypatch, edits, ac, tag):
    # too_soon: 10-8=2 (<3) -> not a review transition; no_edit: nothing edited yet
    monkeypatch.setenv("GT_SELF_VERIFY_ATTEMPTS", "2")
    run = _runner("FAILED x\n__GT_TEST_RC__=1")
    out = _w._maybe_run_presubmit_tests(_cfg(edits=edits, action_count=ac),
                                        SimpleNamespace(content="orig"), run)
    assert run.calls == [], f"{tag}: must not run the suite"
    assert _delivered(out) == "orig"


@skip
def test_one_verify_per_edit_then_rearm_and_cap(monkeypatch):
    monkeypatch.setenv("GT_SELF_VERIFY_ATTEMPTS", "2")
    monkeypatch.setattr(_w, "_GT_BASELINE", False)
    cfg = _cfg(edits=[5], action_count=10)
    run = _runner("FAILED x - AssertionError\n__GT_TEST_RC__=1")
    # attempt 1 on edit@5 -> fires
    _w._maybe_run_presubmit_tests(cfg, SimpleNamespace(content="o"), run)
    assert cfg._retry_attempts_used == 1 and cfg._retry_last_verified_edit == 5
    # same edit, later turn -> already verified, no re-run
    cfg.action_count = 14
    n_before = len(run.calls)
    _w._maybe_run_presubmit_tests(cfg, SimpleNamespace(content="o"), run)
    assert len(run.calls) == n_before, "same edit must not re-verify"
    # NEW edit@15 -> re-arms, attempt 2 fires
    cfg._source_edit_actions.append(15)
    cfg.action_count = 20
    _w._maybe_run_presubmit_tests(cfg, SimpleNamespace(content="o"), run)
    assert cfg._retry_attempts_used == 2
    # third NEW edit -> attempts exhausted (_RETRY_MAX=2) -> quiet
    cfg._source_edit_actions.append(25)
    cfg.action_count = 30
    n_before = len(run.calls)
    _w._maybe_run_presubmit_tests(cfg, SimpleNamespace(content="o"), run)
    assert len(run.calls) == n_before, "attempts exhausted must not run the suite"
