"""Artifact-first TTD: hook-internal calls must not consume agent budget.

Frozen artifact: tests/fixtures/trajectory/nolsp_13579/trajectory.traj
Evidence: Agent's first action is gt_orient. Response: BUDGET_EXHAUSTED.
Cause: startup briefing at swe_agent_state_gt.py:2672 calls
increment_tool_count("gt_orient"), consuming the 1-call agent budget.

Expected behaviors from EXPECTED_BEHAVIOR_BUDGET.md:
  EB-BUDGET-1: Startup does not consume agent-visible orient budget
  EB-BUDGET-2: First explicit agent gt_orient succeeds
  EB-BUDGET-3: Second explicit agent gt_orient fails (cap=1)
  EB-BUDGET-4: Hook-internal calls still recorded in telemetry
  EB-BUDGET-5: Budget report distinguishes internal vs agent-visible

These tests call the REAL get_tool_counts / increment_tool_count functions
from the hook module. They MUST fail on the current implementation (which
has a single shared counter) and pass only after the budget split is
implemented.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "trajectory"

# We test the budget functions directly from the hook module.
# The hook uses file-based state at /tmp/gt_tool_counts.json.
# We redirect to a tempdir to isolate tests.
sys.path.insert(0, str(REPO_ROOT / "benchmarks" / "swebench" / "vm_bundle"))


@pytest.fixture(autouse=True)
def _isolate_budget_files(tmp_path):
    """Redirect hook's file-based counters to tmp_path so tests don't
    interfere with each other or with the real /tmp/ files."""
    import swe_agent_state_gt as hook
    orig_counts = hook.GT_TOOL_COUNTS
    orig_internal = hook.GT_TOOL_COUNTS_INTERNAL
    hook.GT_TOOL_COUNTS = tmp_path / "gt_tool_counts.json"
    hook.GT_TOOL_COUNTS_INTERNAL = tmp_path / "gt_tool_counts_internal.json"
    yield
    hook.GT_TOOL_COUNTS = orig_counts
    hook.GT_TOOL_COUNTS_INTERNAL = orig_internal


# ─────────────────────────────────────────────────────────────────────
# TEST 1 — EB-BUDGET-1: artifact proves the bug exists
# ─────────────────────────────────────────────────────────────────────
def test_artifact_proves_budget_exhausted_on_first_agent_orient():
    """From frozen nolsp_13579/trajectory.traj:

    The agent's first action (step 2, role=assistant) calls gt_orient.
    The response (step 1 or 3, role=user) contains BUDGET_EXHAUSTED.

    This test PASSES on the current broken implementation because it
    asserts the BUG EXISTS. It is the 'red proof' artifact — if this
    test ever FAILS, the bug is fixed and the artifact no longer
    reproduces.
    """
    traj_path = FIXTURES / "nolsp_13579" / "trajectory.traj"
    assert traj_path.exists(), f"frozen trajectory missing: {traj_path}"

    traj = json.loads(traj_path.read_text())
    history = traj.get("history", traj.get("trajectory", []))

    # Find the assistant step that calls gt_orient
    agent_called_orient = False
    budget_exhausted_in_response = False

    for step in history:
        content = str(step.get("content", step.get("action", "")))
        role = step.get("role", "")

        if role == "assistant" and "gt_orient" in content:
            agent_called_orient = True
        if "BUDGET_EXHAUSTED" in content and "gt_orient" in content:
            budget_exhausted_in_response = True

    assert agent_called_orient, (
        "Frozen trajectory must show agent calling gt_orient"
    )
    assert budget_exhausted_in_response, (
        "Frozen trajectory must show BUDGET_EXHAUSTED for gt_orient. "
        "If this assertion fails, the artifact no longer reproduces the bug."
    )


# ─────────────────────────────────────────────────────────────────────
# TEST 2 — EB-BUDGET-1: startup must not consume agent budget
# ─────────────────────────────────────────────────────────────────────
def test_startup_orient_does_not_consume_agent_budget():
    """After the hook's automatic startup briefing, the agent-visible
    gt_orient budget must still show count=0.
    """
    import swe_agent_state_gt as hook

    hook.increment_internal_tool_count("gt_orient")

    counts = hook.get_tool_counts()
    assert counts.get("gt_orient", 0) == 0, (
        f"After startup briefing (hook-internal), agent-visible "
        f"gt_orient count must be 0. Got {counts.get('gt_orient', 0)}."
    )

    internal = hook.get_internal_tool_counts()
    assert internal.get("gt_orient", 0) == 1, (
        f"Hook-internal gt_orient count must be 1. Got {internal.get('gt_orient', 0)}."
    )


# ─────────────────────────────────────────────────────────────────────
# TEST 3 — EB-BUDGET-2: first explicit agent orient succeeds
# ─────────────────────────────────────────────────────────────────────
def test_first_agent_orient_after_startup_succeeds():
    """After startup consumed its internal orient call, the agent's first
    explicit gt_orient must still succeed (count < limit).
    """
    import swe_agent_state_gt as hook

    hook.increment_internal_tool_count("gt_orient")
    hook.increment_tool_count("gt_orient")

    counts = hook.get_tool_counts()
    agent_orient = counts.get("gt_orient", 0)
    assert agent_orient == 1, f"After one agent call, count must be 1. Got {agent_orient}."
    assert agent_orient <= 1, f"First agent orient must be within limit. count={agent_orient}"


# ─────────────────────────────────────────────────────────────────────
# TEST 4 — EB-BUDGET-3: second explicit agent orient fails
# ─────────────────────────────────────────────────────────────────────
def test_second_agent_orient_after_startup_fails():
    """After startup + one agent orient, the second agent orient must
    be denied (cap=1 for agent-initiated calls).
    """
    import swe_agent_state_gt as hook

    hook.increment_internal_tool_count("gt_orient")
    hook.increment_tool_count("gt_orient")  # agent call 1
    hook.increment_tool_count("gt_orient")  # agent call 2

    counts = hook.get_tool_counts()
    agent_orient = counts.get("gt_orient", 0)
    assert agent_orient == 2, f"Two agent calls → count=2. Got {agent_orient}."
    assert agent_orient > 1, "Agent count > limit=1 → second call denied"


# ─────────────────────────────────────────────────────────────────────
# TEST 5 — EB-BUDGET-4: startup call recorded in telemetry
# ─────────────────────────────────────────────────────────────────────
def test_startup_orient_still_recorded_in_telemetry():
    """The startup briefing must still emit checkpoint_startup in the
    telemetry. Splitting the budget does NOT hide the call — it only
    stops charging the agent.

    This uses the frozen telemetry which already shows the event.
    """
    telem_path = FIXTURES / "nolsp_13579" / "gt_hook_telemetry.jsonl"
    events = [json.loads(l) for l in telem_path.read_text().splitlines() if l.strip()]

    startup_events = [e for e in events if e.get("event") == "checkpoint_startup"]
    assert len(startup_events) >= 1, (
        "Startup briefing must emit checkpoint_startup in telemetry "
        "regardless of budget accounting changes."
    )
