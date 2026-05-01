from __future__ import annotations

from groundtruth.runtime.control_policy import decide_control_action, format_intervention


def test_control_policy_blocks_high_risk_visible_drift_when_supported() -> None:
    decision = decide_control_action(
        patch_shape={"warnings": ["root_scaffold_files_added"]},
        replan_decision={
            "should_replan": True,
            "reasons": ["first_edit_root_scaffold"],
            "next_actions": ["Remove root-level repro/scaffold files from the patch."],
        },
        hook_can_block=True,
    )

    assert decision["severity"] == "block"
    assert decision["hook_visible_to_agent"] is True
    assert decision["hook_blocked"] is True
    assert "first_edit_root_scaffold" in decision["reasons"]
    assert "Remove root-level" in format_intervention(decision)


def test_control_policy_warns_without_block_capability() -> None:
    decision = decide_control_action(
        patch_shape={"warnings": ["edits_outside_candidate_cluster"]},
        replan_decision={
            "should_replan": True,
            "reasons": ["three_edits_outside_cluster"],
            "next_actions": ["Re-check localization before continuing."],
        },
        hook_can_block=False,
    )

    assert decision["severity"] == "warn"
    assert decision["hook_visible_to_agent"] is True
    assert decision["hook_blocked"] is False


def test_control_policy_keeps_final_audit_out_of_agent_visibility() -> None:
    decision = decide_control_action(
        patch_shape={"warnings": ["tests_only_patch"]},
        hook_can_block=True,
        final_audit_only=True,
    )

    assert decision["severity"] == "audit"
    assert decision["hook_visible_to_agent"] is False
    assert decision["hook_blocked"] is False
    assert decision["final_audit_only"] is True
    assert format_intervention(decision) == ""


def test_control_policy_compacts_visible_test_failures() -> None:
    decision = decide_control_action(
        test_result={
            "executed": True,
            "all_passed": False,
            "failing_test_names": ["tests/test_core.py::test_contract"],
        },
        hook_can_block=True,
    )

    assert decision["severity"] == "block"
    assert decision["validation_failed"] is True
    assert decision["next_actions"] == [
        "Repair visible failing test first: tests/test_core.py::test_contract."
    ]
