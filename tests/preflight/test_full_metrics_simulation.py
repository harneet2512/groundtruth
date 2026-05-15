"""Full metrics simulation — proves every cell in GOAL_DEEP_LAYER_GROUNDED_METRICS.md is filled.

Simulates a complete 1-task run with all layers emitting structured events,
then verifies the run summary has no blank cells.
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
from groundtruth.telemetry.metrics import compute_run_summary, print_summary


@pytest.fixture
def run_dir():
    d = tempfile.mkdtemp(prefix="gt_sim_")
    yield d


def _simulate_full_run(output_dir: str) -> dict:
    """Simulate a realistic 1-task run with all layers."""
    w = GTTelemetryWriter(run_id="sim_run", task_id="sim_task", output_dir=output_dir)

    # --- L1: Brief injection ---
    l1 = GTLayerEvent(
        layer="L1", event_type="brief_injected", eligible=True, emitted=True, suppressed=False,
        iter=0, max_iter=100,
        rendered_text="1. src/auth.py (validate_token) / Calls: utils.py, cache.py / Tests: test_auth.py",
        event_bucket="ORIENTATION",
        confidence_level="HIGH", confidence_score=0.85, confidence_basis="bm25+graph_reach",
        evidence_items=[
            EvidenceItem(kind="l1_candidate", file_path="src/auth.py", symbol="validate_token").to_dict(),
            EvidenceItem(kind="l1_graph_edge", file_path="src/utils.py", source="CALLS").to_dict(),
            EvidenceItem(kind="l1_test_edge", file_path="tests/test_auth.py").to_dict(),
            EvidenceItem(kind="l1_signature", symbol="validate_token(token: str, strict: bool) -> bool").to_dict(),
        ],
    )
    l1_id = w.emit_layer_event(l1)

    # Belief: candidate
    w.emit_belief_event(GTBeliefEvent(
        file_path="src/auth.py", new_status="candidate", reason="l1_brief",
        source_event_id=l1_id, iter=0,
    ))

    # --- Agent reads brief candidate (L3b fires) ---
    w.emit_agent_event(GTAgentEvent(
        agent_action_id="a1", iter=1, event_bucket="OPEN_INSPECT",
        agent_event_type="file_read", file_path="src/auth.py", file_kind="DURABLE_PRODUCT_FILE",
        max_iter=100,
    ))

    l3b_1 = GTLayerEvent(
        layer="L3b", event_type="post_view_navigation", eligible=True, emitted=True, suppressed=False,
        iter=1, max_iter=100, file_path="src/auth.py",
        rendered_text="Called by: src/utils.py (3x), src/main.py (1x)\nImported by: tests/test_auth.py",
        event_bucket="OPEN_INSPECT",
        evidence_items=[
            EvidenceItem(kind="l3b_caller_edge", file_path="src/utils.py", text="3 calls").to_dict(),
            EvidenceItem(kind="l3b_caller_edge", file_path="src/main.py", text="1 call").to_dict(),
            EvidenceItem(kind="l3b_importer_edge", file_path="tests/test_auth.py").to_dict(),
        ],
    )
    w.emit_layer_event(l3b_1)

    # --- Agent reads caller (L3b fires again) ---
    w.emit_agent_event(GTAgentEvent(
        agent_action_id="a2", iter=2, event_bucket="OPEN_INSPECT",
        agent_event_type="file_read", file_path="src/utils.py", file_kind="DURABLE_PRODUCT_FILE",
        max_iter=100,
    ))

    l3b_2 = GTLayerEvent(
        layer="L3b", event_type="post_view_navigation", eligible=True, emitted=True, suppressed=False,
        iter=2, max_iter=100, file_path="src/utils.py",
        rendered_text="Calls into: src/cache.py (2x)",
        event_bucket="OPEN_INSPECT",
        evidence_items=[
            EvidenceItem(kind="l3b_callee_edge", file_path="src/cache.py", text="2 calls").to_dict(),
        ],
    )
    w.emit_layer_event(l3b_2)

    # --- L6: Reindex before edit ---
    l6 = GTLayerEvent(
        layer="L6", event_type="reindex_complete", eligible=True, emitted=True, suppressed=False,
        iter=4, max_iter=100,
    )
    w.emit_layer_event(l6)

    # --- Agent edits source (L3 fires with next_action) ---
    w.emit_agent_event(GTAgentEvent(
        agent_action_id="a3", iter=5, event_bucket="EDIT_COMMITMENT",
        agent_event_type="source_edit", file_path="src/auth.py", file_kind="DURABLE_PRODUCT_FILE",
        max_iter=100, state_changed=True,
    ))

    l3_1 = GTLayerEvent(
        layer="L3", event_type="post_edit_contract", eligible=True, emitted=True, suppressed=False,
        iter=5, max_iter=100, file_path="src/auth.py",
        rendered_text="Callers:\n  src/utils.py:42: result = validate_token(token, strict=True)\nSignature: validate_token(token: str, strict: bool) -> bool",
        next_action_type="READ_CALLER_CONTRACT", next_action_file="src/utils.py",
        event_bucket="EDIT_COMMITMENT", file_kind="DURABLE_PRODUCT_FILE",
        confidence_level="HIGH", confidence_basis="graph_caller_edge",
        evidence_items=[
            EvidenceItem(kind="l3_caller_code", file_path="src/utils.py", text="validate_token(token, strict=True)", line_start=42).to_dict(),
            EvidenceItem(kind="l3_signature", symbol="validate_token(token: str, strict: bool) -> bool").to_dict(),
        ],
    )
    l3_id = w.emit_layer_event(l3_1)

    # L3 reaction: agent follows
    w.emit_agent_reaction(GTAgentReactionEvent(
        gt_event_id=l3_id, gt_layer="L3", gt_iter=5,
        follow_type="FOLLOWED_EXACT",
        gt_next_action_type="READ_CALLER_CONTRACT", gt_next_action_file="src/utils.py",
        followed_within_1=True, followed_within_3=True,
        opened_suggested_file=True,
    ))

    # Belief: unverified edit
    w.emit_belief_event(GTBeliefEvent(
        file_path="src/auth.py", new_status="unverified", reason="source_edit",
        source_event_id=l3_id, iter=5, previous_status="candidate",
    ))

    # --- Agent reads caller (follows L3) ---
    w.emit_agent_event(GTAgentEvent(
        agent_action_id="a4", iter=6, event_bucket="OPEN_INSPECT",
        agent_event_type="file_read", file_path="src/utils.py", file_kind="DURABLE_PRODUCT_FILE",
        max_iter=100, related_gt_event_id=l3_id,
    ))

    # --- Agent runs search ---
    w.emit_agent_event(GTAgentEvent(
        agent_action_id="a5", iter=7, event_bucket="SEARCH",
        agent_event_type="grep_search", command="grep -r 'validate_token' src/",
        max_iter=100,
    ))

    # --- Agent runs broad test (L5 WEAK_VERIFICATION detected) ---
    w.emit_agent_event(GTAgentEvent(
        agent_action_id="a6", iter=30, event_bucket="VERIFICATION_CHECK",
        agent_event_type="test_run", command="pytest", check_kind="BROAD_CHECK",
        verification_strength="WEAK", max_iter=100,
    ))

    # L3 suppressed (duplicate edit)
    l3_supp = GTLayerEvent(
        layer="L3", event_type="post_edit_dedup", eligible=True, emitted=False, suppressed=True,
        suppression_reason="duplicate", iter=31, max_iter=100, file_path="src/auth.py",
    )
    w.emit_layer_event(l3_supp)

    # L5 goku: WEAK_VERIFICATION (suppressed as MEDIUM in mid band)
    l5_supp = GTLayerEvent(
        layer="L5", event_type="WEAK_VERIFICATION_AFTER_EDIT", eligible=True,
        emitted=False, suppressed=True,
        suppression_reason="confidence_gate:MEDIUM_in_mid_commitment",
        iter=31, max_iter=100,
        event_bucket="VERIFICATION_CHECK", confidence_level="MEDIUM",
        confidence_basis="broad_pass_no_targeted",
    )
    w.emit_layer_event(l5_supp)

    # --- Agent runs targeted test ---
    w.emit_agent_event(GTAgentEvent(
        agent_action_id="a7", iter=35, event_bucket="VERIFICATION_CHECK",
        agent_event_type="test_run", command="pytest tests/test_auth.py -k test_validate",
        check_kind="TARGETED_CHECK", verification_strength="STRONG", max_iter=100,
    ))

    # L5 goku: STRONG_VERIFICATION (state update, no intervention)

    # --- L5b intervention for structural witness ignored (late band) ---
    l5_fire = GTLayerEvent(
        layer="L5", event_type="STRUCTURAL_WITNESS_IGNORED", eligible=True,
        emitted=True, suppressed=False,
        iter=70, max_iter=100,
        rendered_text="[GT L5: Structural Witness Ignored]\nGT suggested READ_CALLER_CONTRACT (src/cache.py) but not inspected.",
        next_action_type="READ_CALLER_CONTRACT", next_action_file="src/cache.py",
        event_bucket="CONTEXT_NAVIGATION", confidence_level="HIGH",
        confidence_basis="3_actions_no_follow",
    )
    l5_id = w.emit_layer_event(l5_fire)

    l5b = GTLayerEvent(
        layer="L5b", event_type="intervention_goku_STRUCTURAL_WITNESS_IGNORED",
        eligible=True, emitted=True, suppressed=False,
        iter=70, max_iter=100, parent_event_id=l5_id,
        rendered_text="[GT L5: Structural Witness Ignored]\nNext action: inspect src/cache.py",
    )
    w.emit_layer_event(l5b)

    # L5 reaction
    w.emit_agent_reaction(GTAgentReactionEvent(
        gt_event_id=l5_id, gt_layer="L5", gt_iter=70,
        follow_type="FOLLOWED_EXACT",
        gt_next_action_type="READ_CALLER_CONTRACT", gt_next_action_file="src/cache.py",
        followed_within_3=True,
    ))

    # --- HYGIENE: scaffold strip at finish ---
    hyg = GTLayerEvent(
        layer="HYGIENE", event_type="scaffold_strip", eligible=True, emitted=True, suppressed=False,
        iter=99, max_iter=100,
    )
    w.emit_layer_event(hyg)

    # --- Agent finish ---
    w.emit_agent_event(GTAgentEvent(
        agent_action_id="a8", iter=99, event_bucket="FINISH_TERMINAL",
        agent_event_type="finish_attempt", max_iter=100,
    ))

    w.close()

    summary = compute_run_summary(
        w.layer_events_path, w.agent_reactions_path,
        w.agent_events_path, w.belief_ledger_path,
    )
    w_reopen = GTTelemetryWriter.__new__(GTTelemetryWriter)
    w_reopen._summary_path = os.path.join(output_dir, "gt_run_summary_sim_task.json")
    w_reopen.write_run_summary(summary)

    return summary


class TestFullMetricsSimulation:
    """Prove every metric cell is filled after a simulated 1-task run."""

    def test_all_layer_sections_present(self, run_dir: str) -> None:
        summary = _simulate_full_run(run_dir)
        for key in ("l1", "l3", "l3b", "l5", "l6", "hygiene", "meta_reaction", "agent_events"):
            assert key in summary, f"Missing section: {key}"

    def test_l1_metrics_filled(self, run_dir: str) -> None:
        s = _simulate_full_run(run_dir)["l1"]
        assert s["l1_brief_generated"] is True
        assert s["l1_brief_injected"] is True
        assert s["l1_candidate_count"] >= 1
        assert len(s["l1_candidate_files"]) >= 1
        assert s["l1_confidence_level"] == "HIGH"
        assert s["l1_confidence_score"] == 0.85
        assert s["l1_rendered_tokens"] > 0
        assert s["l1_gt_pullback_to_l1_count"] == 0

    def test_l3_metrics_filled(self, run_dir: str) -> None:
        s = _simulate_full_run(run_dir)["l3"]
        assert s["l3_edit_events_seen"] >= 1
        assert s["l3_evidence_emitted"] >= 1
        assert s["l3_caller_code_line_count"] >= 1
        assert s["l3_next_action_population_rate"] > 0
        assert s["l3_follow_rate_within_3"] > 0
        assert s["l3_rendered_tokens"] > 0
        assert s["l3_utilization_score"] >= 0.75

    def test_l3b_metrics_filled(self, run_dir: str) -> None:
        s = _simulate_full_run(run_dir)["l3b"]
        assert s["l3b_file_read_events"] >= 1
        assert s["l3b_navigation_emitted"] >= 1
        assert s["l3b_caller_edge_count"] >= 1
        assert s["l3b_avg_chars_per_fire"] > 0
        assert s["l3b_total_chars_per_task"] > 0

    def test_l5_metrics_filled(self, run_dir: str) -> None:
        s = _simulate_full_run(run_dir)["l5"]
        assert s["l5_agent_events_seen_total"] >= 1
        assert s["l5_detection_fired_count"] >= 1
        assert s["l5_detection_suppressed_count"] >= 1
        assert "WEAK_VERIFICATION_AFTER_EDIT" in str(s["l5_agent_events_by_type"]) or "STRUCTURAL_WITNESS_IGNORED" in str(s["l5_agent_events_by_type"])
        assert s["l5_detection_to_l5b_rate"] > 0
        assert s["l5b_messages_emitted"] >= 1
        assert s["l5_detection_to_agent_follow_rate"] > 0

    def test_l6_metrics_filled(self, run_dir: str) -> None:
        s = _simulate_full_run(run_dir)["l6"]
        assert s["l6_reindex_attempt_count"] >= 1
        assert s["l6_reindex_success_count"] >= 1

    def test_hygiene_metrics_filled(self, run_dir: str) -> None:
        s = _simulate_full_run(run_dir)["hygiene"]
        assert s["hygiene_invoked_on_finish"] is True
        assert s["hygiene_scaffold_files_detected"] >= 1

    def test_meta_reaction_metrics_filled(self, run_dir: str) -> None:
        s = _simulate_full_run(run_dir)["meta_reaction"]
        assert s["gt_layer_events_count"] >= 5
        assert s["gt_rendered_messages_count"] >= 3
        assert s["gt_rendered_messages_missing_event_id"] == 0
        assert s["reaction_events_count"] >= 2
        assert s["reaction_coverage_rate"] > 0
        assert s["followed_exact_count"] >= 1
        assert s["event_to_reaction_join_rate"] > 0

    def test_agent_events_filled(self, run_dir: str) -> None:
        s = _simulate_full_run(run_dir)["agent_events"]
        assert s["agent_events_total"] >= 5
        assert "EDIT_COMMITMENT" in s["agent_events_by_bucket"]
        assert "OPEN_INSPECT" in s["agent_events_by_bucket"]
        assert "VERIFICATION_CHECK" in s["agent_events_by_bucket"]
        assert "SEARCH" in s["agent_events_by_bucket"]
        assert "FINISH_TERMINAL" in s["agent_events_by_bucket"]

    def test_proof_spine_passes(self, run_dir: str) -> None:
        summary = _simulate_full_run(run_dir)
        assert summary["proof_spine_pass"], f"Proof spine failed: {summary['proof_spine']}"

    def test_no_hard_fails(self, run_dir: str) -> None:
        summary = _simulate_full_run(run_dir)
        assert summary["run_valid"], f"Hard fails: {summary['hard_fails']}"

    def test_utilization_above_threshold(self, run_dir: str) -> None:
        summary = _simulate_full_run(run_dir)
        for layer in ("L3", "L5"):
            score = summary["per_layer"].get(layer, {}).get("utilization_score", 0)
            assert score >= 0.75, f"{layer} utilization {score} < 0.75"

    def test_run_summary_json_written(self, run_dir: str) -> None:
        summary = _simulate_full_run(run_dir)
        path = os.path.join(run_dir, "gt_run_summary_sim_task.json")
        assert os.path.exists(path)
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["run_valid"]
        assert "l1" in loaded
        assert "l3" in loaded
        assert "l5" in loaded
        assert "meta_reaction" in loaded

    def test_no_blank_metrics_in_summary(self, run_dir: str) -> None:
        """THE KEY TEST: no blank cells in the summary."""
        summary = _simulate_full_run(run_dir)

        blanks = []
        for section_name in ("l1", "l3", "l3b", "l5", "l6", "hygiene", "meta_reaction", "agent_events"):
            section = summary.get(section_name, {})
            for key, value in section.items():
                if value is None:
                    blanks.append(f"{section_name}.{key}")

        assert not blanks, f"Blank metrics found: {blanks}"
