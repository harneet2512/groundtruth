"""Tests for control.decision_log -- IMPLEMENTED, not xfail.

decision_log is the one I/O routing module the kernel ships in Phase 1.
These tests verify:
    1. Every KernelEvent written has all 7 Decision Trace fields populated.
    2. error_class is non-Unknown when the error path is taken.
    3. Round-trip JSON serialization preserves the schema.
    4. append_decision returns None on telemetry-write failure (never raises).
"""

from __future__ import annotations

import json
from pathlib import Path

from groundtruth.control.decision_log import (
    KERNEL_DECISION_BLOCK,
    append_decision,
)
from groundtruth.control.types import (
    AuthorityExercised,
    ContextEvaluated,
    DecisionAction,
    ErrorClass,
    Evidence,
    KernelEvent,
    PolicyApplied,
    TriggeringState,
)


def _make_event(*, error_class: str | None = None) -> KernelEvent:
    return KernelEvent(
        timestamp="2026-04-30T14:22:11Z",
        task_id="t-001",
        triggering_state=TriggeringState(
            scaffold="openhands",
            event_kind="pre_tool",
            tool="str_replace_editor",
            edit_index=0,
        ),
        context_evaluated=ContextEvaluated(
            evidence=Evidence(node_ids=[1, 2], edge_ids=[10]),
            provenance={
                "graph_db_sha": "abc",
                "plan_path": "/tmp/p.json",
                "confidence_components": {"localization": 0.7, "drift": 0.0, "graph_validation": 1.0},
                "error_class": error_class,
            },
        ),
        policy_applied=PolicyApplied(rule_id="first_edit_missed_focus", rule_version="kernel-0.1"),
        alternatives_considered=[
            {"action": "visible", "rejected_because": "confidence>=0.6"},
        ],
        confidence=0.71,
        action_selected=DecisionAction.BLOCK,
        authority_exercised=AuthorityExercised(
            adapter="openhands",
            actual_action=DecisionAction.BLOCK,
            degraded_from=None,
        ),
    )


def test_all_seven_fields_present_on_disk(tmp_path: Path) -> None:
    event = _make_event()
    path = append_decision(event, log_dir=str(tmp_path))
    assert path is not None
    line = Path(path).read_text(encoding="utf-8").splitlines()[-1]
    record = json.loads(line)
    assert record["block"] == KERNEL_DECISION_BLOCK
    inner = record[KERNEL_DECISION_BLOCK]
    for field in (
        "triggering_state",
        "context_evaluated",
        "policy_applied",
        "alternatives_considered",
        "confidence",
        "action_selected",
        "authority_exercised",
    ):
        assert field in inner, f"missing Decision Trace field: {field}"


def test_error_path_uses_non_unknown_class(tmp_path: Path) -> None:
    event = _make_event(error_class=ErrorClass.PROVIDER_ERROR.value)
    path = append_decision(event, log_dir=str(tmp_path))
    assert path is not None
    line = Path(path).read_text(encoding="utf-8").splitlines()[-1]
    record = json.loads(line)
    error_class = record[KERNEL_DECISION_BLOCK]["context_evaluated"]["provenance"]["error_class"]
    assert error_class is not None
    assert error_class != ErrorClass.UNKNOWN.value


def test_unknown_error_class_is_legal_but_alertable(tmp_path: Path) -> None:
    # Unknown is legal at the schema level; the verify_report layer alerts on it.
    event = _make_event(error_class=ErrorClass.UNKNOWN.value)
    path = append_decision(event, log_dir=str(tmp_path))
    assert path is not None


def test_round_trip_preserves_schema(tmp_path: Path) -> None:
    event = _make_event()
    append_decision(event, log_dir=str(tmp_path))
    line = (tmp_path / "gt_runtime_telemetry.jsonl").read_text(encoding="utf-8").splitlines()[0]
    record = json.loads(line)
    inner = record[KERNEL_DECISION_BLOCK]
    assert inner["confidence"] == event.confidence
    assert inner["action_selected"] == event.action_selected.value
    assert inner["authority_exercised"]["adapter"] == "openhands"
    assert inner["authority_exercised"]["actual_action"] == DecisionAction.BLOCK.value
    assert inner["authority_exercised"]["degraded_from"] is None


def test_append_returns_none_on_unwritable_dir(tmp_path: Path) -> None:
    # Pointing at a path that exists as a regular file instead of a directory
    # forces append_block into its OSError branch; it returns None.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("blocker", encoding="utf-8")
    event = _make_event()
    result = append_decision(event, log_dir=str(blocker))
    assert result is None


def test_alternatives_considered_is_list_even_when_empty(tmp_path: Path) -> None:
    base = _make_event()
    event = base.model_copy(update={"alternatives_considered": []})
    path = append_decision(event, log_dir=str(tmp_path))
    assert path is not None
    record = json.loads(Path(path).read_text(encoding="utf-8").splitlines()[-1])
    assert record[KERNEL_DECISION_BLOCK]["alternatives_considered"] == []


def test_b3_concurrent_writes_no_truncation(tmp_path: Path) -> None:
    """B3 mutation pin: 50 concurrent kernel.log calls must produce 50 well-formed lines.

    If FileLock is removed (or its key shrunk to a global lock), interleaving
    can split JSONL records mid-line. This test fails under that mutation.
    """
    import threading

    def _write(i: int) -> None:
        ev = _make_event()
        ev = ev.model_copy(update={"task_id": f"t-{i:03d}", "confidence": float(i) / 100.0})
        append_decision(ev, log_dir=str(tmp_path))

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_path = tmp_path / "gt_runtime_telemetry.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 50, f"expected 50 lines, got {len(lines)}"
    seen_task_ids: set[str] = set()
    for ln in lines:
        record = json.loads(ln)  # raises on truncated/malformed -> test fails
        seen_task_ids.add(record["task_id"])
    assert len(seen_task_ids) == 50


def test_degraded_from_round_trips(tmp_path: Path) -> None:
    base = _make_event()
    event = base.model_copy(
        update={
            "authority_exercised": AuthorityExercised(
                adapter="openhands",
                actual_action=DecisionAction.VISIBLE,
                degraded_from=DecisionAction.BLOCK,
            ),
        }
    )
    path = append_decision(event, log_dir=str(tmp_path))
    assert path is not None
    record = json.loads(Path(path).read_text(encoding="utf-8").splitlines()[-1])
    auth = record[KERNEL_DECISION_BLOCK]["authority_exercised"]
    assert auth["actual_action"] == DecisionAction.VISIBLE.value
    assert auth["degraded_from"] == DecisionAction.BLOCK.value
