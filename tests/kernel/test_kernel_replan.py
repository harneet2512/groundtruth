"""RED tests for kernel.replan.

Pin layers:
    1. Happy -- drift signal triggers a corrective replan.
    2. Boundary -- no triggers stays_course; multiple triggers but no candidates available.
    3. Adversarial -- repeated identical triggers (idempotency); validation+drift both firing.
    4. Mutation -- documented per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundtruth.control import kernel
from groundtruth.control.types import (
    BriefResult,
    Candidate,
    Capabilities,
    DriftSignals,
    Evidence,
    ReplanStage,
    ReplanTriggers,
    RunState,
    ValidationResult,
)


# Phase 1 implementation landed -- tests are now expected-pass.


def _rs(focus: list[str], cluster: list[str]) -> RunState:
    focus_paths = [Path(p) for p in focus]
    cluster_paths = [Path(p) for p in cluster]
    return RunState(
        task_id="t",
        plan={"agent_focus_files": focus, "cluster_files": cluster},
        brief_result=BriefResult(
            brief_text="",
            candidates=[Candidate(path=Path(p), score=0.8) for p in focus[:3]],
            focus_files=focus_paths[:3],  # BriefResult schema caps at 3
            cluster_files=cluster_paths,
            confidence=0.75,
            plan={},
            plan_path=None,
        ),
        edit_history=[],
        capabilities=Capabilities(block=True, visible=True, audit=True, mid_task_pull=True, replan_inject=True),
    )


# Layer 1: Happy -- single drift signal produces a corrective replan.
# Mutation pin: any branch that returns stay_course when first_edit_misses_focus is True.
def test_first_edit_misses_focus_triggers_corrective():
    rs = _rs(["src/a.py"], ["src/a.py", "src/b.py"])
    triggers = ReplanTriggers(
        drift=DriftSignals(first_edit_misses_focus=True),
    )
    plan = kernel.replan(triggers, rs)
    assert plan.stage == ReplanStage.CORRECTIVE


# Layer 2: Boundary -- empty triggers stay_course.
# Mutation pin: defaulting to corrective when no signals fire.
def test_no_triggers_stays_course():
    rs = _rs(["src/a.py"], ["src/a.py"])
    triggers = ReplanTriggers(drift=DriftSignals())
    plan = kernel.replan(triggers, rs)
    assert plan.stage == ReplanStage.STAY_COURSE


# Layer 2: Boundary -- recompute-class triggers escalate from corrective to recompute.
# Mutation pin: missing escalation rules for "no_focus_file_after_three_edits".
def test_no_focus_after_three_recomputes():
    rs = _rs(["src/a.py"], ["src/a.py"])
    triggers = ReplanTriggers(
        drift=DriftSignals(edits_outside_cluster_count=3, repeated_warnings=["no_focus_file_after_three_edits"]),
    )
    plan = kernel.replan(triggers, rs)
    assert plan.stage == ReplanStage.RECOMPUTE


# Layer 2: Boundary -- next_actions list is capped at 3.
# Mutation pin: missing the [:3] slice.
def test_next_actions_capped_at_three():
    rs = _rs(["src/a.py"], ["src/a.py"])
    triggers = ReplanTriggers(
        drift=DriftSignals(
            first_edit_misses_focus=True,
            root_scaffold_added=True,
            edits_outside_cluster_count=5,
            repeated_warnings=["a", "b", "c", "d"],
        ),
        validation=ValidationResult(ok=False, broken_signatures=["foo"], evidence=Evidence()),
        failing_tests_after_edit=True,
    )
    plan = kernel.replan(triggers, rs)
    assert len(plan.next_actions) <= 3


# Layer 3: Adversarial -- triggers fired but no candidates available.
# Mutation pin: KeyError on missing candidates.
def test_triggers_with_no_candidates_does_not_crash():
    rs = RunState(
        task_id="t",
        plan={"agent_focus_files": [], "cluster_files": []},
        brief_result=None,
        edit_history=[],
        capabilities=Capabilities(block=True, visible=True, audit=True, mid_task_pull=True, replan_inject=True),
    )
    triggers = ReplanTriggers(drift=DriftSignals(first_edit_misses_focus=True))
    plan = kernel.replan(triggers, rs)
    assert plan.stage in {ReplanStage.CORRECTIVE, ReplanStage.RECOMPUTE}


# Layer 3: Adversarial -- repeated identical triggers must be idempotent.
# Calling replan twice with the same triggers must produce the same stage.
# Mutation pin: any internal counter that mutates RunState.
def test_idempotent_under_repeated_triggers():
    rs = _rs(["src/a.py"], ["src/a.py"])
    triggers = ReplanTriggers(drift=DriftSignals(first_edit_misses_focus=True))
    plan_a = kernel.replan(triggers, rs)
    plan_b = kernel.replan(triggers, rs)
    assert plan_a.stage == plan_b.stage
    assert plan_a.message == plan_b.message


# Layer 3: Adversarial -- validation failure with no drift produces corrective replan
# anchored on graph evidence (per ADR 0002 -- graph validation is a primary signal).
# Mutation pin: ignoring validation when drift is empty.
def test_validation_only_failure_corrects():
    rs = _rs(["src/a.py"], ["src/a.py"])
    triggers = ReplanTriggers(
        drift=DriftSignals(),
        validation=ValidationResult(
            ok=False,
            broken_signatures=["src.a.foo"],
            orphaned_callers=["src.b.bar"],
            evidence=Evidence(),
        ),
    )
    plan = kernel.replan(triggers, rs)
    assert plan.stage == ReplanStage.CORRECTIVE


# Layer 3: Adversarial -- agent_focus_files capped at 3 and never empty
# when focus files exist in the plan.
# Mutation pin: returning [] when focus_files is non-empty.
def test_agent_focus_files_propagated():
    rs = _rs(["src/a.py", "src/b.py", "src/c.py", "src/d.py"], ["src/a.py"])
    triggers = ReplanTriggers(drift=DriftSignals(first_edit_misses_focus=True))
    plan = kernel.replan(triggers, rs)
    assert 1 <= len(plan.agent_focus_files) <= 3
