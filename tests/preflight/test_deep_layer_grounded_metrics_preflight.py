"""Offline preflight for deep_layer_grounded metrics — Case 12.

Validates: every rendered message has event_id, every next_action has reaction,
every suppression has reason, utilization scores computed, proof spine passes.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from groundtruth.telemetry.schemas import (
    GTLayerEvent, GTAgentReactionEvent, GTBeliefEvent, GTAgentEvent, EvidenceItem,
)
from groundtruth.telemetry.writer import GTTelemetryWriter
from groundtruth.telemetry.metrics import (
    compute_run_summary, compute_proof_spine, compute_hard_fails,
    compute_layer_utilization, print_summary,
)
from groundtruth.telemetry.constants import VALID_L5_EVENT_TYPES


@pytest.fixture
def output_dir() -> str:
    d = tempfile.mkdtemp(prefix="gt_preflight_")
    yield d


@pytest.fixture
def writer(output_dir: str) -> GTTelemetryWriter:
    w = GTTelemetryWriter(run_id="preflight_run", task_id="test_task", output_dir=output_dir)
    yield w
    w.close()


class TestCase12MetricsCompleteness:
    """Synthetic run with all layers emitting — validates proof spine."""

    def _emit_synthetic_run(self, writer: GTTelemetryWriter) -> None:
        """Emit a realistic synthetic run across all layers."""
        # L1 brief
        l1_event = GTLayerEvent(
            layer="L1", event_type="brief_injected", eligible=True, emitted=True,
            suppressed=False, iter=0, max_iter=100,
            rendered_text="1. src/auth.py (validate_token) / Calls: utils.py",
            evidence_items=[
                EvidenceItem(kind="l1_candidate", file_path="src/auth.py", symbol="validate_token").to_dict(),
            ],
        )
        l1_id = writer.emit_layer_event(l1_event)

        # L3 post-edit with next_action
        l3_event = GTLayerEvent(
            layer="L3", event_type="post_edit_evidence", eligible=True, emitted=True,
            suppressed=False, iter=5, max_iter=100,
            file_path="src/auth.py",
            rendered_text="Callers: utils.py:42 calls validate_token(strict=True)",
            next_action_type="READ_CALLER_CONTRACT",
            next_action_file="src/utils.py",
            evidence_items=[
                EvidenceItem(kind="l3_caller_code", file_path="src/utils.py", text="validate_token(strict=True)").to_dict(),
            ],
            event_bucket="EDIT_COMMITMENT",
            confidence_level="HIGH",
        )
        l3_id = writer.emit_layer_event(l3_event)

        # L3 reaction
        writer.emit_agent_reaction(GTAgentReactionEvent(
            gt_event_id=l3_id, gt_layer="L3", gt_iter=5,
            follow_type="FOLLOWED_EXACT",
            gt_next_action_type="READ_CALLER_CONTRACT",
            gt_next_action_file="src/utils.py",
            followed_within_3=True,
            opened_suggested_file=True,
        ))

        # L3b post-view
        l3b_event = GTLayerEvent(
            layer="L3b", event_type="post_view_navigation", eligible=True, emitted=True,
            suppressed=False, iter=6, max_iter=100,
            file_path="src/utils.py",
            rendered_text="Called by: src/main.py (2x)",
            evidence_items=[
                EvidenceItem(kind="l3b_caller_edge", file_path="src/main.py").to_dict(),
            ],
        )
        writer.emit_layer_event(l3b_event)

        # L5 goku event (suppressed — medium confidence in early band)
        l5_suppressed = GTLayerEvent(
            layer="L5", event_type="WEAK_VERIFICATION_AFTER_EDIT", eligible=True,
            emitted=False, suppressed=True,
            suppression_reason="confidence_gate:MEDIUM_in_early_exploration",
            iter=8, max_iter=100,
            event_bucket="VERIFICATION_CHECK",
            confidence_level="MEDIUM",
        )
        writer.emit_layer_event(l5_suppressed)

        # L5 goku event (fired — high confidence in late band)
        l5_fired = GTLayerEvent(
            layer="L5", event_type="STRUCTURAL_WITNESS_IGNORED", eligible=True,
            emitted=True, suppressed=False,
            iter=65, max_iter=100,
            rendered_text="[GT L5: Structural Witness Ignored]\nEvidence: GT suggested READ_CALLER_CONTRACT (src/callers.py) but it was not inspected.",
            next_action_type="READ_CALLER_CONTRACT",
            next_action_file="src/callers.py",
            event_bucket="CONTEXT_NAVIGATION",
            confidence_level="HIGH",
            confidence_basis="3_actions_no_follow",
        )
        l5_id = writer.emit_layer_event(l5_fired)

        # L5b intervention
        l5b_event = GTLayerEvent(
            layer="L5b", event_type="intervention_goku_STRUCTURAL_WITNESS_IGNORED",
            eligible=True, emitted=True, suppressed=False,
            iter=65, max_iter=100,
            parent_event_id=l5_id,
            rendered_text="[GT L5: Structural Witness Ignored]\nNext action: inspect src/callers.py",
        )
        l5b_id = writer.emit_layer_event(l5b_event)

        # L5 reaction
        writer.emit_agent_reaction(GTAgentReactionEvent(
            gt_event_id=l5_id, gt_layer="L5", gt_iter=65,
            follow_type="FOLLOWED_EXACT",
            gt_next_action_type="READ_CALLER_CONTRACT",
            gt_next_action_file="src/callers.py",
            followed_within_3=True,
        ))

        # L6 reindex
        writer.emit_layer_event(GTLayerEvent(
            layer="L6", event_type="reindex_complete", eligible=True, emitted=True,
            suppressed=False, iter=4, max_iter=100,
        ))

        # HYGIENE
        writer.emit_layer_event(GTLayerEvent(
            layer="HYGIENE", event_type="scaffold_strip", eligible=True, emitted=True,
            suppressed=False, iter=99, max_iter=100,
        ))

        # Agent events
        for i, (bucket, etype) in enumerate([
            ("ORIENTATION", "plan_created"),
            ("SEARCH", "grep_search"),
            ("OPEN_INSPECT", "file_read"),
            ("EDIT_COMMITMENT", "source_edit"),
            ("VERIFICATION_CHECK", "test_run"),
            ("FINISH_TERMINAL", "finish_attempt"),
        ]):
            writer.emit_agent_event(GTAgentEvent(
                agent_action_id=f"act_{i}", iter=i * 15,
                event_bucket=bucket, agent_event_type=etype,
                max_iter=100,
            ))

    def test_proof_spine_passes(self, writer: GTTelemetryWriter, output_dir: str) -> None:
        self._emit_synthetic_run(writer)
        writer.close()

        summary = compute_run_summary(
            writer.layer_events_path,
            writer.agent_reactions_path,
            writer.agent_events_path,
        )

        assert summary["proof_spine_pass"], f"Proof spine failed: {summary['proof_spine']}"

    def test_no_hard_fails(self, writer: GTTelemetryWriter, output_dir: str) -> None:
        self._emit_synthetic_run(writer)
        writer.close()

        summary = compute_run_summary(
            writer.layer_events_path,
            writer.agent_reactions_path,
        )

        assert summary["run_valid"], f"Hard fails: {summary['hard_fails']}"

    def test_utilization_scores_computed(self, writer: GTTelemetryWriter, output_dir: str) -> None:
        self._emit_synthetic_run(writer)
        writer.close()

        summary = compute_run_summary(
            writer.layer_events_path,
            writer.agent_reactions_path,
        )

        for layer in ("L3", "L5"):
            assert layer in summary["per_layer"]
            score = summary["per_layer"][layer]["utilization_score"]
            assert score >= 0.75, f"{layer} utilization {score} < 0.75"

    def test_every_emitted_event_has_id(self, writer: GTTelemetryWriter, output_dir: str) -> None:
        self._emit_synthetic_run(writer)
        writer.close()

        with open(writer.layer_events_path, encoding="utf-8") as f:
            for line in f:
                event = json.loads(line)
                if event.get("emitted"):
                    assert event.get("event_id"), f"Emitted event missing event_id: {event}"

    def test_every_suppression_has_reason(self, writer: GTTelemetryWriter, output_dir: str) -> None:
        self._emit_synthetic_run(writer)
        writer.close()

        with open(writer.layer_events_path, encoding="utf-8") as f:
            for line in f:
                event = json.loads(line)
                if event.get("suppressed"):
                    assert event.get("suppression_reason"), f"Suppressed without reason: {event}"

    def test_every_next_action_has_reaction(self, writer: GTTelemetryWriter, output_dir: str) -> None:
        self._emit_synthetic_run(writer)
        writer.close()

        events_with_next_action = []
        with open(writer.layer_events_path, encoding="utf-8") as f:
            for line in f:
                event = json.loads(line)
                if event.get("next_action_type"):
                    events_with_next_action.append(event)

        reaction_ids = set()
        with open(writer.agent_reactions_path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                reaction_ids.add(r.get("gt_event_id"))

        for event in events_with_next_action:
            assert event["event_id"] in reaction_ids, (
                f"next_action event {event['event_id']} (layer={event['layer']}) has no reaction"
            )

    def test_l5_event_types_are_generalized(self, writer: GTTelemetryWriter, output_dir: str) -> None:
        self._emit_synthetic_run(writer)
        writer.close()

        with open(writer.layer_events_path, encoding="utf-8") as f:
            for line in f:
                event = json.loads(line)
                if event.get("layer") == "L5":
                    et = event.get("event_type", "")
                    for fw in ("pytest", "jest", "cargo", "go_test", "npm_test"):
                        assert fw not in et.lower(), f"L5 event type {et} contains framework name"

    def test_run_summary_written(self, writer: GTTelemetryWriter, output_dir: str) -> None:
        self._emit_synthetic_run(writer)
        writer.close()

        summary = compute_run_summary(
            writer.layer_events_path,
            writer.agent_reactions_path,
            writer.agent_events_path,
        )
        writer.write_run_summary(summary)

        assert os.path.exists(writer.run_summary_path)
        with open(writer.run_summary_path) as f:
            loaded = json.load(f)
        assert loaded["run_valid"]
        assert loaded["total_layer_events"] > 0
        assert loaded["total_reactions"] > 0


class TestSchemaValidation:
    """Validate schema constraints are enforced."""

    def test_invalid_event_bucket_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid event_bucket"):
            GTAgentEvent(
                agent_action_id="test", iter=0,
                event_bucket="PYTEST_FAILED",
                max_iter=100,
            )

    def test_invalid_file_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid file_kind"):
            GTAgentEvent(
                agent_action_id="test", iter=0,
                event_bucket="EDIT_COMMITMENT",
                file_kind="PYTEST_FILE",
                max_iter=100,
            )

    def test_invalid_layer_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid layer"):
            GTLayerEvent(
                layer="PYTEST", event_type="test", eligible=True,
                emitted=True, suppressed=False,
            )

    def test_valid_l5_event_type(self) -> None:
        event = GTLayerEvent(
            layer="L5", event_type="STRUCTURAL_WITNESS_IGNORED",
            eligible=True, emitted=True, suppressed=False,
            iter=50, max_iter=100,
            event_bucket="CONTEXT_NAVIGATION",
            confidence_level="HIGH",
        )
        assert event.event_id
        assert event.schema_version
