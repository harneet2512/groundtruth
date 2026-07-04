"""Mock trajectory tests — verify GT telemetry schemas for realistic scenarios.

These tests validate that the telemetry dataclasses correctly represent
the 10 canonical GT-agent interaction patterns from the Ultraplan spec.
"""
from __future__ import annotations

from groundtruth.telemetry.schemas import (
    GTLayerEvent, GTAgentReactionEvent, GTBeliefEvent, EvidenceKind,
)
from groundtruth.telemetry.evidence import make_evidence_item
from groundtruth.trajectory.hooks import L5bSafetyChecker


class TestMockTrajectories:

    def test_01_l1_right_agent_follows(self):
        """L1 suggests file X. Agent reads X, edits X."""
        event = GTLayerEvent(
            layer="L1", event_type="localization_brief",
            eligible=True, emitted=True, suppressed=False,
            evidence_items=[
                make_evidence_item(kind=EvidenceKind.L1_CANDIDATE.value, file_path="src/auth.py", confidence=0.85),
            ],
        )
        assert event.event_id
        belief = GTBeliefEvent(
            file_path="src/auth.py", new_status="candidate",
            reason="L1 identified via graph_db", source_event_id=event.event_id,
        )
        assert belief.new_status == "candidate"
        reaction = GTAgentReactionEvent(
            gt_event_id=event.event_id, gt_layer="L1", gt_iter=0,
            follow_type="FOLLOWED_EXACT", followed_within_1=True,
            opened_suggested_file=True,
        )
        assert reaction.followed_within_1

    def test_02_l1_wrong_agent_finds_better(self):
        """L1 suggests X but agent edits Y → Y promoted, X stale."""
        event = GTLayerEvent(
            layer="L1", event_type="localization_brief",
            eligible=True, emitted=True, suppressed=False,
        )
        stale = GTBeliefEvent(
            file_path="src/wrong.py", new_status="stale",
            reason="agent moved to different file", source_event_id=event.event_id,
        )
        promoted = GTBeliefEvent(
            file_path="src/better.py", new_status="promoted",
            reason="non-L1 file promoted by agent behavior", source_event_id=event.event_id,
        )
        assert stale.new_status == "stale"
        assert promoted.new_status == "promoted"

    def test_03_l3_targeted_verification_followed(self):
        """L3 shows caller code + suggests targeted test. Agent runs it."""
        evidence = [
            make_evidence_item(kind=EvidenceKind.L3_CALLER_CODE.value, file_path="src/bar.py", text="foo(x, y)"),
            make_evidence_item(kind=EvidenceKind.L3_TARGETED_VERIFICATION.value, file_path="tests/test_foo.py", text="pytest tests/test_foo.py"),
        ]
        event = GTLayerEvent(
            layer="L3", event_type="post_edit_contract",
            eligible=True, emitted=True, suppressed=False,
            evidence_items=evidence,
            next_action_type="run_targeted_test",
            next_action_test="pytest tests/test_foo.py",
        )
        assert event.next_action_type == "run_targeted_test"
        assert len(event.evidence_items) == 2
        assert evidence[0]["kind"] == "l3_caller_code"
        assert evidence[0]["text"] == "foo(x, y)"

        reaction = GTAgentReactionEvent(
            gt_event_id=event.event_id, gt_layer="L3", gt_iter=5,
            follow_type="FOLLOWED_EXACT",
            followed_within_1=True, ran_targeted_test_after_gt=True,
        )
        assert reaction.ran_targeted_test_after_gt

    def test_04_l3b_edge_followed(self):
        """L3b shows caller edge. Agent follows to that caller."""
        event = GTLayerEvent(
            layer="L3b", event_type="navigation",
            eligible=True, emitted=True, suppressed=False,
            evidence_items=[
                make_evidence_item(kind=EvidenceKind.L3B_CALLER_EDGE.value, file_path="src/caller.py"),
            ],
        )
        reaction = GTAgentReactionEvent(
            gt_event_id=event.event_id, gt_layer="L3b", gt_iter=8,
            follow_type="FOLLOWED_EXACT",
            followed_within_3=True, opened_suggested_file=True,
        )
        assert reaction.opened_suggested_file

    def test_05_l5b_unverified_patch_ignored(self):
        """L5b emits unverified_patch. Agent ignores and finishes."""
        l5_event = GTLayerEvent(
            layer="L5", event_type="unverified_patch",
            eligible=True, emitted=True, suppressed=False,
        )
        l5b_event = GTLayerEvent(
            layer="L5b", event_type="intervention_unverified_patch",
            parent_event_id=l5_event.event_id,
            eligible=True, emitted=True, suppressed=False,
            next_action_type="run_targeted_test",
            rendered_text="[GT L5: Unverified Patch]\nNext action: run targeted test.",
        )
        reaction = GTAgentReactionEvent(
            gt_event_id=l5b_event.event_id, gt_layer="L5b", gt_iter=20,
            follow_type="IGNORED",
            followed_within_1=False, finished_without_follow=True, ignored=True,
        )
        assert reaction.ignored
        assert reaction.finished_without_follow

    def test_06_l5b_unverified_patch_followed(self):
        """L5b emits unverified_patch. Agent runs targeted test."""
        event = GTLayerEvent(
            layer="L5b", event_type="intervention_unverified_patch",
            eligible=True, emitted=True, suppressed=False,
            next_action_type="run_targeted_test",
        )
        reaction = GTAgentReactionEvent(
            gt_event_id=event.event_id, gt_layer="L5b", gt_iter=20,
            follow_type="FOLLOWED_EXACT",
            followed_within_1=True, ran_targeted_test_after_gt=True,
        )
        assert reaction.followed_within_1
        belief = GTBeliefEvent(
            file_path="src/auth.py", new_status="verified",
            reason="targeted test passed after L5b intervention",
            source_event_id=event.event_id,
        )
        assert belief.new_status == "verified"

    def test_07_broad_pass_not_verified(self):
        """Broad pytest passes. Patch stays unverified."""
        event = GTLayerEvent(
            layer="L5", event_type="broad_verification_after_edit",
            eligible=True, emitted=True, suppressed=False,
            verification_kind="broad_project_verification",
            evidence_sources={"current_patch_verified_status": "broad_pass_only"},
        )
        assert event.verification_kind == "broad_project_verification"
        assert event.evidence_sources["current_patch_verified_status"] == "broad_pass_only"
        belief = GTBeliefEvent(
            file_path="src/auth.py", new_status="unverified",
            reason="broad test pass does not verify patch",
            source_event_id=event.event_id,
        )
        assert belief.new_status == "unverified"

    def test_08_finish_unverified_patch(self):
        """Agent finishes with unverified patch → L5 detects unsafe_finish."""
        event = GTLayerEvent(
            layer="L5", event_type="unsafe_finish",
            eligible=True, emitted=True, suppressed=False,
            evidence_sources={"current_patch_verified_status": "not_verified"},
        )
        l5b = GTLayerEvent(
            layer="L5b", event_type="intervention_unsafe_finish",
            parent_event_id=event.event_id,
            eligible=True, emitted=True, suppressed=False,
            next_action_type="run_targeted_test",
        )
        assert l5b.parent_event_id == event.event_id
        assert l5b.next_action_type == "run_targeted_test"

    def test_09_late_l5b_no_restart(self):
        """At step 75/100, L5b fires. Must not restart or explore broadly."""
        text = (
            "[GT L5: Unverified Patch]\n"
            "Iteration: 75/100\n"
            "Evidence: broad test suite passed after editing src/auth.py.\n"
            "Next action: run a test that specifically exercises the changed function.\n"
            "Do not restart exploration. Repair the current hypothesis."
        )
        is_safe, reason = L5bSafetyChecker.validate(text, 0.75)
        assert is_safe, f"Late L5b message should be safe but got: {reason}"

        event = GTLayerEvent(
            layer="L5b", event_type="intervention_unverified_patch",
            eligible=True, emitted=True, suppressed=False,
            iter=75, max_iter=100,
            rendered_text=text,
            evidence_sources={
                "append_only_confirmed": True,
                "restart_language_present": False,
                "system_prompt_mutated": False,
            },
        )
        assert event.iteration_band == "late_60_85"
        assert event.evidence_sources["restart_language_present"] is False

    def test_10_l6_failure_degrades_safely(self):
        """L6 reindex fails. L3 still fires with stale notice."""
        l6_event = GTLayerEvent(
            layer="L6", event_type="reindex",
            eligible=True, emitted=True, suppressed=False,
            evidence_sources={
                "reindex_success": False,
                "failure_reason": "timeout",
                "stale_index_detected": True,
            },
        )
        l3_event = GTLayerEvent(
            layer="L3", event_type="post_edit_contract",
            eligible=True, emitted=True, suppressed=False,
            evidence_sources={"stale_index": True},
        )
        assert l6_event.evidence_sources["reindex_success"] is False
        assert l3_event.evidence_sources["stale_index"] is True
