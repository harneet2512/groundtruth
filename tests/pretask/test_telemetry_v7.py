"""Telemetry contract tests for v7-ready pre-task logging."""

from __future__ import annotations

from groundtruth.pretask.telemetry import (
    TelemetryRecord,
    empty_v7_cochange_block,
    empty_v7_constraints_block,
    empty_v7_contract_block,
)


def test_v7_empty_blocks_are_json_record_fields() -> None:
    """The base telemetry record carries v7 fields even before v7 runs."""
    record = TelemetryRecord(
        task_id="t",
        timestamp="2026-04-30T00:00:00Z",
        module_7_cochange=empty_v7_cochange_block(),
        module_7_contract=empty_v7_contract_block(),
        module_7_constraints=empty_v7_constraints_block(),
    )

    out = record.as_dict()
    assert out["module_7_cochange"]["enabled"] is False
    assert out["module_7_contract"]["abstain_reason"] == "not_implemented"
    assert "final_*.py" in out["module_7_constraints"]["scaffold_patterns"]


def test_v7_blocks_have_auditable_defaults() -> None:
    """Empty v7 telemetry distinguishes disabled work from missing logging."""
    cochange = empty_v7_cochange_block()
    contract = empty_v7_contract_block()
    constraints = empty_v7_constraints_block()

    assert cochange["cluster_files"] == []
    assert cochange["abstain_reason"] == "not_implemented"
    assert contract["test_files_considered"] == []
    assert contract["extraction_mode"] == "not_implemented"
    assert constraints["hook_warning_fired"] is False
    assert "vendor/" in constraints["negative_space_patterns"]
