from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_feature_inventory as inventory  # noqa: E402  # pyright: ignore[reportMissingImports]
import ss_live_diagnosis as diagnosis  # noqa: E402  # pyright: ignore[reportMissingImports]
from groundtruth.runtime.fact_registry import (  # noqa: E402
    registration_for,
    required_event,
)
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    build_lineage,
    lineage_ledger_extra,
)


def _metric(value, status="MEASURED") -> dict:
    return {"value": value, "status": status}


def _lineage(feature: str, family: str = "FACT", *, causal: bool = False) -> dict:
    return {
        "schema": "gt.feature_lineage.v1",
        "features": [{"category": family, "feature_id": feature,
                      "role": "fact" if family == "FACT" else "byte_owner"}],
        "causal_contribution_proven": causal,
    }


def _delivered(feature: str, *, causal: bool = False) -> dict:
    registration = registration_for(feature)
    assert registration is not None
    lineage = build_lineage(
        runtime_producer_id=registration.producer,
        evidence_type=feature,
        actual_event=required_event(feature) or registration.deliver_by,
    )
    assert lineage is not None
    return {
        "outcome": "delivered", "content_sha256_16": "a" * 16,
        "chars_delivered": 12, **lineage_ledger_extra(lineage),
    }


def _entry(feature: str, receipt: int, *, causal: bool = False) -> dict:
    return {
        "source": "trajectory", "joined": True, "join_method": "seal",
        "receipt": receipt, "feature_lineage": _lineage(feature, causal=causal),
    }


def _suppressed(feature: str, reason: str) -> dict:
    row = _delivered(feature)
    row.update({"outcome": "suppressed_hidden_only", "reason": reason})
    return row


def test_raw_ledger_lineage_requires_registry_validated_flattened_schema() -> None:
    forged = {
        "outcome": "delivered",
        "content_sha256_16": "a" * 16,
        "chars_delivered": 12,
        "lineage": _lineage("caller_contract"),
    }

    assert diagnosis._lineage_from_row(forged) is None
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT",
        {"eligible": _metric(True), "produced": _metric(True)},
        {"gates": {}}, [forged], [],
    ) == "UNMEASURED:missing_lineage"


def test_support_and_control_roles_do_not_borrow_delivery_lineage() -> None:
    support = {
        "family": "ACQ", "status": "MEASURED", "source": "acq_provenance",
        "receipt_level": 2, "block_id": "file-entry-1",
        "content_sha256_16": "b" * 16,
        "ss_readiness": {
            "role": "support", "live_witness": False, "ss_live": False,
            "gates": {
                "supported_fact_delivery_join": True,
                "candidate_local_contribution": True,
                "source_contribution_correct": None,
                "timing_inherited_from_fact_delivery": None,
                "source_causal_fair_probe": None,
            },
        },
    }
    mediator = {
        "family": "CAP", "status": "MEASURED",
        "ss_readiness": {
            "role": "infra_control", "live_witness": False, "ss_live": False,
            "gates": {
                "runtime_member_control_receipt": None,
                "mediated_fact_ids": True,
                "mediation_correct": None,
                "mediation_causal_fair_probe": None,
            },
        },
    }

    assert diagnosis.classify_typed_terminal(support, "support") == (
        "UNMEASURED:support:source_contribution_correct"
    )
    assert diagnosis.classify_typed_terminal(mediator, "infra_control") == (
        "UNMEASURED:infra_control:runtime_member_control_receipt"
    )
    assert diagnosis.classify_cap_control(
        "GT_OBLIGATION_FRESHNESS", mediator
    ) == "UNMEASURED:eligibility_control:terminal_contract_unavailable"

    failed = json.loads(json.dumps(support))
    failed["ss_readiness"]["gates"]["source_contribution_correct"] = False
    assert diagnosis.classify_typed_terminal(failed, "support") == (
        "FAILED:support:source_contribution_correct"
    )
    live = json.loads(json.dumps(support))
    live["ss_readiness"]["gates"] = {
        gate: True for gate in diagnosis.TYPED_TERMINAL_GATES["support"]
    }
    live["ss_readiness"].update({"live_witness": True, "ss_live": True})
    assert diagnosis.classify_typed_terminal(live, "support") == "SUPPORT_SS_LIVE"


def test_exact_inventory_and_run_population_fail_closed(tmp_path: Path) -> None:
    inventory_map = inventory.canonical_feature_inventory()
    with pytest.raises(ValueError, match="exact-128"):
        diagnosis._validate_task_metrics(
            {"ss_features": {}, "ss_integrity": {}}, inventory_map,
            tmp_path / "metrics.json",
        )
    with pytest.raises(ValueError, match="population/integrity"):
        diagnosis._validate_run_metrics_population({
            "mandatory_performance_collection_complete": True,
            "tasks": 1,
            "task_population": {
                "observed_record_count": 1, "observed_unique_count": 1,
                "missing_tasks": ["missing"], "duplicate_tasks": [],
                "unexpected_tasks": [], "invalid_task_records": [],
            },
        }, 1)
    exact_rows = {
        name: {"family": family}
        for family, names in inventory_map.items() for name in names
    }
    diagnosis._validate_task_metrics({
        "ss_features": exact_rows,
        "ss_integrity": {
            "inventory_complete": True, "required_inputs_complete": False,
        },
    }, inventory_map, tmp_path / "metrics.json")
    diagnosis._validate_run_metrics_population({
        "mandatory_performance_collection_complete": False,
        "tasks": 1,
        "task_population": {
            "observed_record_count": 1, "observed_unique_count": 1,
            "missing_tasks": [], "duplicate_tasks": [],
            "unexpected_tasks": [], "invalid_task_records": [],
        },
    }, 1)
    (tmp_path / "gt_deep_metrics_repo__task-1.json").write_text(json.dumps({
        "schema": "gt_deep_metrics.v2", "task_id": "repo__task-1",
    }), encoding="utf-8")
    diagnosis._validate_deep_metric_tasks(tmp_path, {"repo__task-1"})
    with pytest.raises(ValueError, match="do not match"):
        diagnosis._validate_deep_metric_tasks(tmp_path, {"repo__task-2"})


def test_delivery_bucket_precedence_and_missing_lineage_fail_closed() -> None:
    lifecycle = {
        "eligible": _metric(True), "produced": _metric(True),
        "delivered": _metric(True), "truth_valid": _metric(True),
        "authority_valid": _metric(True), "expired_late": _metric(False),
        "stale": _metric(False), "receipt_level": _metric(3),
    }
    readiness = {"gates": {
        "delivered_byte_proven": True, "correct_info": True,
        "correct_rl_adhered_time": True, "acknowledged": True,
        "leak_zero": True, "dose_lte_one": True, "fair_probe": True,
    }}

    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle, readiness, [], []
    ) == "UNMEASURED:missing_lineage"
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle, readiness,
        [_suppressed("caller_contract", "ss_step_behind")], []
    ) == "STEP_BEHIND"
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle, readiness,
        [_delivered("caller_contract")], [_entry("caller_contract", 1)],
    ) == "NOVEL_IGNORED"
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle, readiness,
        [_delivered("caller_contract", causal=True)],
        [_entry("caller_contract", 3, causal=True)],
    ) == "CAUSAL_P5"


def test_not_eligible_and_explicit_production_states() -> None:
    readiness = {"gates": {}}
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", {"eligible": _metric(False)}, readiness, [], []
    ) == "NOT_ELIGIBLE"
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT",
        {"eligible": _metric(True), "produced": _metric(False)},
        {"lineage_complete": True, "gates": {}}, [], [],
    ) == "DARK_ELIGIBLE_NO_PRODUCER"
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT",
        {"eligible": _metric(True), "produced": _metric(True)}, readiness,
        [_suppressed("caller_contract", "global_arbiter:outranked")], [],
    ) == "PRODUCED_NOT_DELIVERED"
    assert diagnosis.classify_delivery_feature(
        "GT_GATEWAY", "CAP",
        {"eligible": _metric(True), "produced": _metric(True)}, readiness,
        [{"outcome": "delivered", "lineage": {
            "schema": "gt.feature_lineage.v1",
            "features": [{"category": "CAP", "feature_id": "GT_GATEWAY",
                          "role": "mediator"}],
        }}], [],
    ) == "UNMEASURED:missing_lineage"


def test_wrong_late_sealed_and_acknowledged_buckets() -> None:
    lifecycle = {
        "eligible": _metric(True), "produced": _metric(True),
        "delivered": _metric(True), "truth_valid": _metric(True),
        "authority_valid": _metric(True), "expired_late": _metric(False),
        "stale": _metric(False),
    }
    gates = {
        "delivered_byte_proven": True, "correct_info": True,
        "correct_rl_adhered_time": True, "acknowledged": True,
        "leak_zero": True, "dose_lte_one": True, "fair_probe": None,
    }
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle,
        {"gates": {**gates, "correct_info": False}},
        [_delivered("caller_contract")], [_entry("caller_contract", 2)],
    ) == "WRONG_INFO"
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle,
        {"gates": {**gates, "correct_rl_adhered_time": False}},
        [_delivered("caller_contract")], [_entry("caller_contract", 2)],
    ) == "LATE"
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle,
        {"gates": {**gates, "correct_info": None}},
        [_delivered("caller_contract")], [_entry("caller_contract", 2)],
    ) == "SEALED_DELIVERED_UNGRADED"
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle, {"gates": gates},
        [_delivered("caller_contract")], [_entry("caller_contract", 2)],
    ) == "ACKNOWLEDGED"


def test_diagnosis_emits_exact_inventory_and_perf_statuses(tmp_path: Path) -> None:
    inv = inventory.canonical_feature_inventory()
    task = "repo__task-1"
    task_dir = tmp_path / task
    task_dir.mkdir()
    ss_features = {}
    for family, names in inv.items():
        for name in names:
            ss_features[name] = {
                "family": family,
                "status": "NOT_APPLICABLE" if family == "PERF" else "MEASURED",
                "ss_readiness": {"gates": {}},
            }
    first_acq = inv["ACQ"][0]
    ss_features[first_acq].update({
        "source": "acq_provenance", "receipt_level": 2,
        "block_id": "file-entry-1", "content_sha256_16": "c" * 16,
        "ss_readiness": {
            "role": "support", "live_witness": False, "ss_live": False,
            "gates": {
                "supported_fact_delivery_join": True,
                "candidate_local_contribution": True,
                "source_contribution_correct": None,
                "timing_inherited_from_fact_delivery": None,
                "source_causal_fair_probe": None,
            },
        },
    })
    ss_features["GT_GATEWAY"]["ss_readiness"] = {
        "role": "infra_control", "live_witness": False, "ss_live": False,
        "gates": {
            "runtime_member_control_receipt": None, "mediated_fact_ids": True,
            "mediation_correct": None, "mediation_causal_fair_probe": None,
        },
    }
    feature_path = task_dir / f"gt_feature_metrics_{task}.json"
    feature_path.write_text(json.dumps({
        "schema": "gt.feature_metrics.v1", "task": task,
        "ss_features": ss_features, "features": {}, "fact_classes": {},
        "ss_integrity": {
            "inventory_complete": True, "required_inputs_complete": True,
        },
    }), encoding="utf-8")
    (task_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({"messages": []}), encoding="utf-8"
    )
    (task_dir / f"gt_runtime_ledger_{task}.jsonl").write_text("", encoding="utf-8")
    (task_dir / "brief_result.json").write_text("{}", encoding="utf-8")
    (task_dir / f"gt_deep_metrics_{task}.json").write_text(json.dumps({
        "schema": "gt_deep_metrics.v2", "task_id": task,
    }), encoding="utf-8")

    mandatory = {}
    for section, definitions in inventory.performance_metric_definitions().items():
        mandatory[section] = {
            name: {"status": "MEASURED" if name == "gold_rank" else "NOT_APPLICABLE"}
            for name, _ in definitions
        }
    run_metrics = tmp_path / "gt_run_metrics_v2_run.json"
    run_metrics.write_text(json.dumps({
        "schema": "gt_run_metrics.v2", "mandatory_performance_metric_count": 58,
        "mandatory_performance": mandatory,
        "mandatory_performance_collection_complete": True,
        "tasks": 1,
        "task_population": {
            "observed_record_count": 1, "observed_unique_count": 1,
            "missing_tasks": [], "duplicate_tasks": [], "unexpected_tasks": [],
            "invalid_task_records": [],
        },
    }), encoding="utf-8")

    result = diagnosis.diagnose_run(tmp_path, run_metrics)

    assert result["feature_count"] == 128
    assert len(result["rows"]) == 128
    assert [(row["family"], row["feature"]) for row in result["rows"]] == [
        (family, name) for family, names in inv.items() for name in names
    ]
    by_name = {row["feature"]: row for row in result["rows"]}
    assert by_name["gold_rank"]["run_bucket"] == "MEASURED"
    assert by_name["patch_size"]["run_bucket"] == "NOT_APPLICABLE"
    assert by_name["caller_contract"]["task_buckets"][task] == "UNMEASURED:missing_lineage"
    assert by_name[first_acq]["task_buckets"][task] == (
        "UNMEASURED:support:source_contribution_correct"
    )
    assert by_name["GT_GATEWAY"]["task_buckets"][task] == (
        "UNMEASURED:infra_control:runtime_member_control_receipt"
    )
    assert by_name["GT_OBLIGATION_FRESHNESS"]["task_buckets"][task] == (
        "UNMEASURED:eligibility_control:terminal_contract_unavailable"
    )
    assert diagnosis._perf_status({"status": "PARTIAL"}) == "FAILED"
    assert diagnosis.render_markdown(result).count("\n") == 130
    output = tmp_path / "diagnosis.json"
    assert diagnosis.main([
        str(tmp_path), "--run-metrics", str(run_metrics),
        "--output", str(output),
    ]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["feature_count"] == 128

    incomplete_task = json.loads(feature_path.read_text(encoding="utf-8"))
    incomplete_task["ss_integrity"]["required_inputs_complete"] = False
    feature_path.write_text(json.dumps(incomplete_task), encoding="utf-8")
    incomplete_run = json.loads(run_metrics.read_text(encoding="utf-8"))
    incomplete_run["mandatory_performance_collection_complete"] = False
    run_metrics.write_text(json.dumps(incomplete_run), encoding="utf-8")

    incomplete = diagnosis.diagnose_run(tmp_path, run_metrics)
    incomplete_by_name = {row["feature"]: row for row in incomplete["rows"]}
    assert incomplete["feature_count"] == 128
    assert incomplete_by_name[first_acq]["task_buckets"][task] == (
        "UNMEASURED:required_inputs_incomplete"
    )
    assert incomplete_by_name["caller_contract"]["task_buckets"][task] == (
        "UNMEASURED:required_inputs_incomplete"
    )
    assert incomplete_by_name["gold_rank"]["run_bucket"] == "MEASURED"


def test_task_artifacts_build_exact_seal_join_with_typed_lineage(tmp_path: Path) -> None:
    task = "repo__typed"
    metrics_path = tmp_path / f"gt_feature_metrics_{task}.json"
    metrics_path.write_text("{}", encoding="utf-8")
    payload = "src/pkg.py:12 preserve parse_config callers"
    lineage = {
        "lineage_schema": "gt.feature_lineage.v1",
        "runtime_producer_id": "contract_map",
        "registered_producer_id": "contract_map",
        "producer_registration_match": True,
        "evidence_type": "caller_contract",
        "fact_class": "caller_contract",
        "feature_ids": [{"category": "FACT", "feature_id": "caller_contract",
                         "role": "fact"}],
        "required_event": "file_view", "actual_event": "file_view",
        "receipt_predicate": "preserved_caller_contract",
        "causal_eval": "paired_contract_break_rate", "causal_probe_id": "",
        "causal_contribution_proven": False, "reactive": False,
    }
    row = {
        "layer": "l3.contract", "event_type": "file_view",
        "outcome": "delivered", "chars_delivered": len(payload),
        "content_sha256_16": hashlib.sha256(payload.encode()).hexdigest()[:16],
        "file_path": "src/pkg.py", **lineage,
    }
    (tmp_path / f"gt_runtime_ledger_{task}.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8"
    )
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(json.dumps({
        "messages": [{"role": "tool", "content": payload}],
    }), encoding="utf-8")

    rows, entries, error = diagnosis._task_artifacts(metrics_path, {})

    assert error is None
    assert rows == [row]
    assert len(entries) == 1
    assert entries[0]["join_method"] == "seal"
    assert entries[0]["feature_lineage"]["fact_class"] == "caller_contract"
