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


def _write_binding_artifacts(task_dir: Path, run_id: str = "run-1") -> None:
    artifacts = task_dir / "gt_artifacts"
    artifacts.mkdir(exist_ok=True)
    members = list(inventory.canonical_feature_inventory()["CAP"])
    digest = "ghcr.io/example/gt-substrate@sha256:" + "d" * 64
    (artifacts / "gt_run_identity.json").write_text(json.dumps({
        "schema": "gt.run_identity.v1",
        "substrate_digest_expected": digest,
        "substrate_digest_actual": digest,
        "gt_ref_requested": "a" * 40,
        "gt_ref_prepared_sha": "a" * 40,
        "gt_ref_resolved": "a" * 40,
        "seam_sha256": "b" * 64,
        "runner_sha256": "c" * 64,
        "workflow_run_id": run_id,
        "baseline": False,
    }), encoding="utf-8")
    (artifacts / "gt_profile_activation.json").write_text(json.dumps({
        "schema": "gt.profile_activation.v1",
        "profile": "2",
        "members": members,
    }), encoding="utf-8")
    (artifacts / "gt_profile_receipt.json").write_text(json.dumps({
        "schema": "gt.profile_receipt.v1",
        "gt_rl_profile": "2",
        "members_on": members,
        "member_source": {member: "inherited" for member in members},
        "patched_classes": ["minisweagent.environments.local.LocalEnvironment"],
        "pid": 123,
    }), encoding="utf-8")
    _write_completion_receipt(task_dir, run_id)


def _completion_artifact_names(task: str) -> tuple[str, ...]:
    return (
        "mini-swe-agent.trajectory.json",
        "brief_result.json",
        "task_truth.json",
        "gt_artifacts/gt_run_identity.json",
        "gt_artifacts/gt_agent_exit.json",
        "gt_artifacts/gt_profile_activation.json",
        "gt_artifacts/gt_profile_receipt.json",
        "gt_artifacts/gt_batch_activation.json",
        f"gt_runtime_ledger_{task}.jsonl",
        f"gt_runtime_ledger_attestation_{task}.json",
        f"gt_deep_metrics_{task}.json",
        f"gt_feature_metrics_{task}.json",
        f"gt_performance_metrics_{task}.json",
        f"gt_behavioral_impact_{task}.json",
    )


def _write_completion_receipt(task_dir: Path, run_id: str = "run-1") -> None:
    task = task_dir.name
    artifacts = task_dir / "gt_artifacts"
    artifacts.mkdir(exist_ok=True)
    defaults = {
        "mini-swe-agent.trajectory.json": {"messages": []},
        "brief_result.json": {},
        "task_truth.json": {"schema": "gt.task_truth.v1", "instance_id": task},
        "gt_artifacts/gt_agent_exit.json": {
            "schema": "gt.agent_exit.v1", "task": task, "return_code": 0,
            "trajectory_present": True,
        },
        "gt_artifacts/gt_batch_activation.json": {
            "schema": "gt.batch_activation.v1", "required": True,
            "wrapper_attached": True, "result": "installed", "mini_swe_version": "2.4.5",
        },
        f"gt_runtime_ledger_{task}.jsonl": "",
        f"gt_runtime_ledger_attestation_{task}.json": {},
        f"gt_deep_metrics_{task}.json": {},
        f"gt_feature_metrics_{task}.json": {},
        f"gt_performance_metrics_{task}.json": {},
        f"gt_behavioral_impact_{task}.json": {},
    }
    for relative, value in defaults.items():
        path = task_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            continue
        path.write_text(
            value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
        )
    hashes = {
        relative: hashlib.sha256((task_dir / relative).read_bytes()).hexdigest()
        for relative in _completion_artifact_names(task)
    }
    (task_dir / "gt_task_completion.json").write_text(json.dumps({
        "schema": "gt.task_completion.v1", "task": task,
        "workflow_run_id": run_id, "status": "COMPLETE",
        "artifact_sha256": hashes,
    }), encoding="utf-8")


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
            # B-TERM writer truth: 3 completeness gates; the causal probe is a
            # separate enrichment field, never a gate (NO-GO defect 1 re-pin).
            "role": "infra_control", "live_witness": False, "ss_live": False,
            "gates": {
                "runtime_member_control_receipt": None,
                "mediated_fact_ids": True,
                "mediation_correct": None,
            },
            "mediation_causal_fair_probe": None,
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
    with pytest.raises(ValueError, match="exact-129"):
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
    assert diagnosis._validate_run_metrics_population({
        "mandatory_performance_collection_complete": False,
        "tasks": 1,
        "task_population": {
            "observed_record_count": 1, "observed_unique_count": 1,
            "missing_tasks": [], "duplicate_tasks": [],
            "unexpected_tasks": [], "invalid_task_records": [],
        },
    }, 1) is False

    assert diagnosis._validate_run_metrics_population({
        "mandatory_performance_collection_complete": False,
        "tasks": 1,
        "task_population": {
            "expected_count": 2,
            "observed_record_count": 1, "observed_unique_count": 1,
            "missing_tasks": ["repo__missing"], "duplicate_tasks": [],
            "unexpected_tasks": [], "invalid_task_records": [],
        },
    }, 2) is False
    (tmp_path / "gt_deep_metrics_repo__task-1.json").write_text(json.dumps({
        "schema": "gt_deep_metrics.v2", "task_id": "repo__task-1",
    }), encoding="utf-8")
    assert diagnosis._validate_deep_metric_tasks(
        tmp_path, ("repo__task-1",)
    ) == {"missing": [], "duplicate": [], "unexpected": []}
    assert diagnosis._validate_deep_metric_tasks(
        tmp_path, ("repo__task-2",)
    ) == {
        "missing": ["repo__task-2"],
        "duplicate": [],
        "unexpected": ["repo__task-1"],
    }


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
    # D-M/D-P: "delivered but not acknowledged" is authored from the CLASS grader
    # gate (acknowledged=False), not the generic ladder receipt level.
    readiness_unack = {"gates": dict(readiness["gates"], acknowledged=False)}
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle, readiness_unack,
        [_delivered("caller_contract")], [_entry("caller_contract", 1)],
    ) == "NOVEL_IGNORED"
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle, readiness,
        [_delivered("caller_contract", causal=True)],
        [_entry("caller_contract", 3, causal=True)],
    ) == "CAUSAL_P5"


def test_acknowledged_authored_from_grader_not_ladder_receipt() -> None:
    # D-M/D-P (arviz/aiogram/gitingest/loguru): a generic consumption-ladder ACTED
    # (receipt=3) must NOT terminal ACKNOWLEDGED when the class-specific receipt
    # grader (readiness gates["acknowledged"]) rejects it. The ladder over-credited
    # any later mutation/prose naming a delivered entity with no timing/non-
    # reacquisition/pre-commit gate.
    lifecycle = {
        "eligible": _metric(True), "produced": _metric(True),
        "delivered": _metric(True), "truth_valid": _metric(True),
        "authority_valid": _metric(True), "expired_late": _metric(False),
        "stale": _metric(False), "receipt_level": _metric(3),
    }
    base = {"delivered_byte_proven": True, "correct_info": True,
            "correct_rl_adhered_time": True, "leak_zero": True,
            "dose_lte_one": True, "fair_probe": True}
    entries = [_entry("caller_contract", 3)]           # generic ladder = ACTED
    delivered = [_delivered("caller_contract")]

    # grader False -> NOVEL_IGNORED despite ladder receipt=3 (RED: old code = ACKNOWLEDGED)
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle,
        {"gates": dict(base, acknowledged=False)}, delivered, entries,
    ) == "NOVEL_IGNORED"
    # grader None -> fail-closed (a missing grade never promotes)
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle,
        {"gates": dict(base, acknowledged=None)}, delivered, entries,
    ) == "SEALED_DELIVERED_UNGRADED"
    # grader True -> ACKNOWLEDGED
    assert diagnosis.classify_delivery_feature(
        "caller_contract", "FACT", lifecycle,
        {"gates": dict(base, acknowledged=True)}, delivered, entries,
    ) == "ACKNOWLEDGED"


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
            "mediation_correct": None,
        },
        "mediation_causal_fair_probe": None,
    }
    feature_path = task_dir / f"gt_feature_metrics_{task}.json"
    feature_path.write_text(json.dumps({
        "schema": "gt.feature_metrics.v1", "task": task,
        "ss_features": ss_features, "features": {}, "fact_classes": {},
        "ss_integrity": {
            "inventory_complete": True, "required_inputs_complete": True,
            "missing_required_inputs": [], "missing_feature_inputs": [],
            "visible_audit_complete": True,
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
    _write_binding_artifacts(task_dir)

    mandatory = {}
    gold_rank_aggregate = {
        "status": "MEASURED", "value_type": "nonnegative_int",
        "aggregation": "mean_median_over_measured_tasks",
        "mean": 2.0, "median": 2.0, "measured_tasks": 1,
        "event_observed_tasks": [task], "not_applicable_tasks": [],
        "right_censored_tasks": [], "missing_tasks": [],
        "unmeasured_tasks": [], "failed_tasks": [],
    }
    for section, definitions in inventory.performance_metric_definitions().items():
        mandatory[section] = {
            name: (
                gold_rank_aggregate
                if name == "gold_rank"
                else {"status": "NOT_APPLICABLE"}
            )
            for name, _ in definitions
        }
    run_metrics = tmp_path / "gt_run_metrics_v2_run.json"
    run_metrics.write_text(json.dumps({
        "schema": "gt_run_metrics.v2", "run_id": "run-1",
        "mandatory_performance_metric_count": 58,
        "mandatory_performance": mandatory,
        "mandatory_performance_collection_complete": True,
        # NO-GO defect 5: a MEASURED run PERF row is adjudicated on the writer's
        # SS-MEASURE readiness (gt_feature_metrics ~4371); status alone never promotes.
        "ss_features": {
            "gold_rank": {
                "family": "PERF",
                "ss_readiness": {
                    "role": "measurement", "live_witness": False, "ss_live": False,
                    "gates": {
                        "artifact_valid": True, "metric_structure_valid": True,
                        "precision_8dp": True, "formula_provenance": True,
                        "denominator_provenance": True,
                        "applicability_resolved": True, "task_coverage": True,
                        "aggregate_coverage": True,
                    },
                },
            },
        },
        "tasks": 1,
        "task_population": {
            "observed_record_count": 1, "observed_unique_count": 1,
            "missing_tasks": [], "duplicate_tasks": [], "unexpected_tasks": [],
            "invalid_task_records": [],
        },
    }), encoding="utf-8")

    result = diagnosis.diagnose_run(tmp_path, run_metrics)

    assert result["feature_count"] == 129
    assert result["integrity"]["publishable"] is True
    assert result["integrity"]["identity_profile_binding_complete"] is True
    assert result["integrity"]["tasks"][task]["status"] == "BOUND"
    assert len(result["rows"]) == 129
    assert [(row["family"], row["feature"]) for row in result["rows"]] == [
        (family, name) for family, names in inv.items() for name in names
    ]
    by_name = {row["feature"]: row for row in result["rows"]}
    assert by_name["gold_rank"]["run_bucket"] == "MEASURED"
    assert by_name["gold_rank"]["run_measurement"] == gold_rank_aggregate
    assert by_name["gold_rank"]["task_measurements"][task]["status"] == "NOT_APPLICABLE"
    assert by_name["patch_size"]["run_bucket"] == "NOT_APPLICABLE"
    assert by_name["caller_contract"]["task_buckets"][task] == (
        "UNMEASURED:no_bound_opportunity"
    )
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
    assert diagnosis._perf_status({"status": "RIGHT_CENSORED"}) == "RIGHT_CENSORED"
    # NO-GO defect 5: a MEASURED claim without (or with an unsound) SS-MEASURE
    # readiness fails closed instead of passing on self-declared status.
    assert diagnosis._perf_status({"status": "MEASURED"}) == (
        "FAILED:measurement:readiness_role"
    )
    sound = by_name["gold_rank"]["run_measurement"]
    unsound_readiness = {
        "role": "measurement", "live_witness": False, "ss_live": False,
        "gates": {
            "artifact_valid": True, "metric_structure_valid": True,
            "precision_8dp": False, "formula_provenance": True,
            "denominator_provenance": True, "applicability_resolved": True,
            "task_coverage": True, "aggregate_coverage": True,
        },
    }
    assert diagnosis._perf_status(
        {**sound, "ss_readiness": unsound_readiness}, scope="run",
    ) == "FAILED:measurement:precision_8dp"
    # aggregate_coverage is a run-population gate: it never bites at task grain.
    task_grain_readiness = {
        "role": "measurement", "live_witness": False, "ss_live": False,
        "gates": {
            "artifact_valid": True, "metric_structure_valid": True,
            "precision_8dp": True, "formula_provenance": True,
            "denominator_provenance": True, "applicability_resolved": True,
            "task_coverage": True, "aggregate_coverage": False,
        },
    }
    assert diagnosis._perf_status(
        {"status": "MEASURED", "ss_readiness": task_grain_readiness}
    ) == "MEASURED"
    assert diagnosis._perf_status(
        {"status": "MEASURED", "ss_readiness": task_grain_readiness}, scope="run",
    ) == "FAILED:measurement:aggregate_coverage"
    # T2-audit finding 5: every row carries the ONE executable promotion authority —
    # promoted is False in offline diagnosis, and the declared-vs-implemented schema
    # delta is surfaced so a typed *_SS_LIVE bucket can never be read as promotion.
    for row in result["rows"]:
        promotion = row["promotion"]
        assert promotion["promoted"] is False
        assert promotion["authority"] == "ss_proof_manifest.live_proof_dependencies"
    gateway_promotion = by_name["GT_GATEWAY"]["promotion"]
    assert gateway_promotion["schema_delta"] == ["mediation_causal_fair_probe"]
    # After evidence-backed alias reconciliation the ONLY eligibility gap left is the
    # causal probe (the interim-terminal gap); decision_correct/opportunity_receipt are
    # enforced through G1 mediation_correct + the defect-4 transaction boundary.
    eligibility_promotion = by_name["GT_OBLIGATION_FRESHNESS"]["promotion"]
    assert eligibility_promotion["schema_delta"] == ["eligibility_causal_fair_probe"]
    # Direct byte-owners reconcile fully: promotable in principle from a live run.
    assert by_name["GT_EDIT_CHECK"]["promotion"]["schema_delta"] == []
    # R1 closed by code verification: the FACT runtime-ownership quartet is enforced
    # inside gates 1+3, so direct FACT rows reconcile fully too.
    assert by_name["caller_contract"]["promotion"]["schema_delta"] == []
    # SS-MEASURE reconciles except the paired-delta requirement.
    assert by_name["gold_rank"]["promotion"]["schema_delta"] == [
        "matched_delta_when_required"
    ]
    assert by_name["cochange_prior"]["promotion"]["implemented_terminal_schema"] == list(
        diagnosis.TYPED_TERMINAL_GATES["internal_support"]
    )
    rendered = diagnosis.render_markdown(result)
    assert rendered.count("\n") == 140
    assert '"mean":2.0' in rendered
    assert "requested_tasks=1" in rendered
    assert "missing_feature_records=NONE" in rendered
    output = tmp_path / "diagnosis.json"
    assert diagnosis.main([
        str(tmp_path), "--run-metrics", str(run_metrics),
        "--output", str(output),
    ]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["feature_count"] == 129

    expected_tasks = tmp_path / "expected_tasks.json"
    missing_task = "repo__pre-agent-failure"
    expected_tasks.write_text(json.dumps({
        "task_ids": [task, missing_task],
    }), encoding="utf-8")
    (tmp_path / f"gt_deep_metrics_{missing_task}.json").write_text(json.dumps({
        "schema": "gt_deep_metrics.v2", "task_id": missing_task,
    }), encoding="utf-8")
    expected_run = json.loads(run_metrics.read_text(encoding="utf-8"))
    expected_run["tasks"] = 2
    expected_run["task_population"].update({
        "expected_count": 2,
        "observed_record_count": 2,
        "observed_unique_count": 2,
    })
    run_metrics.write_text(json.dumps(expected_run), encoding="utf-8")

    scoped = diagnosis.diagnose_run(
        tmp_path, run_metrics, expected_tasks_path=expected_tasks,
    )
    scoped_by_name = {row["feature"]: row for row in scoped["rows"]}
    assert scoped["task_count"] == 2
    assert scoped["observed_task_count"] == 1
    assert scoped["missing_feature_metric_tasks"] == [missing_task]
    assert scoped["missing_deep_metric_tasks"] == []
    assert scoped["integrity"]["deep_metric_population"] == {
        "missing": [], "duplicate": [], "unexpected": [],
    }
    assert scoped["integrity"]["publishable"] is False
    assert scoped["integrity"]["tasks"][missing_task] == {
        "status": "UNMEASURED",
        "issues": ["feature_metrics:missing"],
        "artifacts": {},
        "identity": {},
        "profile": {},
    }
    assert scoped_by_name["caller_contract"]["task_buckets"][missing_task] == (
        "UNMEASURED:missing_feature_metrics"
    )
    assert scoped_by_name["gold_rank"]["task_buckets"][missing_task] == (
        "UNMEASURED:missing_feature_metrics"
    )

    duplicate_dir = tmp_path / "duplicate"
    duplicate_dir.mkdir()
    (duplicate_dir / f"gt_feature_metrics_{task}.json").write_text(
        feature_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    unexpected_task = "repo__unexpected"
    unexpected_record = json.loads(feature_path.read_text(encoding="utf-8"))
    unexpected_record["task"] = unexpected_task
    unexpected_dir = tmp_path / "unexpected"
    unexpected_dir.mkdir()
    (unexpected_dir / f"gt_feature_metrics_{unexpected_task}.json").write_text(
        json.dumps(unexpected_record), encoding="utf-8"
    )

    contaminated = diagnosis.diagnose_run(
        tmp_path, run_metrics, expected_tasks_path=expected_tasks,
    )
    contaminated_by_name = {row["feature"]: row for row in contaminated["rows"]}
    assert contaminated["integrity"]["publishable"] is False
    assert contaminated["duplicate_feature_metric_tasks"] == [task]
    assert contaminated["unexpected_feature_metric_tasks"] == [unexpected_task]
    assert contaminated_by_name["caller_contract"]["task_buckets"][task] == (
        "UNMEASURED:duplicate_feature_metrics"
    )
    assert unexpected_task not in contaminated_by_name["caller_contract"]["task_buckets"]
    contaminated_markdown = diagnosis.render_markdown(contaminated)
    assert "observed_feature_records=3" in contaminated_markdown
    assert "usable_expected_task_records=0" in contaminated_markdown
    assert f"duplicate_feature_records={task}" in contaminated_markdown
    assert f"unexpected_feature_records={unexpected_task}" in contaminated_markdown

    (duplicate_dir / f"gt_feature_metrics_{task}.json").unlink()
    duplicate_dir.rmdir()
    (unexpected_dir / f"gt_feature_metrics_{unexpected_task}.json").unlink()
    unexpected_dir.rmdir()

    (tmp_path / f"gt_deep_metrics_{missing_task}.json").unlink()

    expected_run["tasks"] = 1
    expected_run["task_population"].update({
        "expected_count": 1,
        "observed_record_count": 1,
        "observed_unique_count": 1,
    })
    run_metrics.write_text(json.dumps(expected_run), encoding="utf-8")

    incomplete_task = json.loads(feature_path.read_text(encoding="utf-8"))
    incomplete_task["ss_integrity"]["required_inputs_complete"] = False
    incomplete_task["ss_integrity"]["missing_feature_inputs"] = [
        "caller_breakage_count",
    ]
    feature_path.write_text(json.dumps(incomplete_task), encoding="utf-8")
    incomplete_run = json.loads(run_metrics.read_text(encoding="utf-8"))
    incomplete_run["mandatory_performance_collection_complete"] = False
    run_metrics.write_text(json.dumps(incomplete_run), encoding="utf-8")

    incomplete = diagnosis.diagnose_run(tmp_path, run_metrics)
    incomplete_by_name = {row["feature"]: row for row in incomplete["rows"]}
    assert incomplete["feature_count"] == 129
    assert incomplete_by_name[first_acq]["task_buckets"][task] == (
        "UNMEASURED:support:source_contribution_correct"
    )
    assert incomplete_by_name["caller_contract"]["task_buckets"][task] == (
        "UNMEASURED:no_bound_opportunity"
    )
    assert incomplete_by_name["gold_rank"]["run_bucket"] == "MEASURED"

    visible_incomplete = json.loads(feature_path.read_text(encoding="utf-8"))
    visible_incomplete["ss_integrity"]["visible_audit_complete"] = False
    visible_incomplete["ss_integrity"]["missing_required_inputs"] = ["visible_audit"]
    feature_path.write_text(json.dumps(visible_incomplete), encoding="utf-8")

    fail_closed = diagnosis.diagnose_run(tmp_path, run_metrics)
    fail_closed_by_name = {row["feature"]: row for row in fail_closed["rows"]}
    assert fail_closed_by_name[first_acq]["task_buckets"][task] == (
        "UNMEASURED:visible_audit_incomplete"
    )
    assert fail_closed_by_name["caller_contract"]["task_buckets"][task] == (
        "UNMEASURED:visible_audit_incomplete"
    )
    assert fail_closed_by_name["GT_GATEWAY"]["task_buckets"][task] == (
        "UNMEASURED:infra_control:runtime_member_control_receipt"
    )


def test_diagnosis_integrity_fails_closed_on_missing_or_mismatched_profile_receipt(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "repo__task-1"
    task_dir.mkdir()
    _write_binding_artifacts(task_dir)
    (task_dir / "gt_artifacts" / "gt_profile_receipt.json").unlink()

    integrity = diagnosis._diagnosis_integrity(
        {"repo__task-1": (task_dir / "gt_feature_metrics_repo__task-1.json", {})},
        {"run_id": "run-1"},
        inventory.canonical_feature_inventory()["CAP"],
    )

    assert integrity["publishable"] is False
    assert integrity["identity_profile_binding_complete"] is False
    task = integrity["tasks"]["repo__task-1"]
    assert task["status"] == "UNMEASURED"
    assert "missing:gt_profile_receipt.json" in task["issues"]
    assert "task_completion:artifact_missing:gt_artifacts/gt_profile_receipt.json" in task["issues"]

    _write_binding_artifacts(task_dir)
    receipt_path = task_dir / "gt_artifacts" / "gt_profile_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["gt_rl_profile"] = "1"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    integrity = diagnosis._diagnosis_integrity(
        {"repo__task-1": (task_dir / "gt_feature_metrics_repo__task-1.json", {})},
        {"run_id": "run-1"},
        inventory.canonical_feature_inventory()["CAP"],
    )
    assert integrity["publishable"] is False
    assert "profile_receipt:profile_mismatch" in integrity["tasks"][
        "repo__task-1"
    ]["issues"]


def test_diagnosis_integrity_requires_task_completion_receipt_and_bound_hashes(
    tmp_path: Path,
) -> None:
    task = "repo__task-1"
    task_dir = tmp_path / task
    task_dir.mkdir()
    _write_binding_artifacts(task_dir)
    receipt_path = task_dir / "gt_task_completion.json"
    receipt_path.unlink()

    missing = diagnosis._diagnosis_integrity(
        {task: (task_dir / f"gt_feature_metrics_{task}.json", {})},
        {"run_id": "run-1"}, inventory.canonical_feature_inventory()["CAP"],
    )
    assert missing["publishable"] is False
    assert "task_completion:missing" in missing["tasks"][task]["issues"]

    _write_completion_receipt(task_dir)
    (task_dir / "task_truth.json").write_text("{}", encoding="utf-8")
    tampered = diagnosis._diagnosis_integrity(
        {task: (task_dir / f"gt_feature_metrics_{task}.json", {})},
        {"run_id": "run-1"}, inventory.canonical_feature_inventory()["CAP"],
    )
    assert tampered["publishable"] is False
    assert "task_completion:artifact_hash:task_truth.json" in tampered["tasks"][task]["issues"]


def test_task_completion_receipt_seals_trajectory_brief_and_run_identity(tmp_path: Path) -> None:
    task = "repo__task-1"
    task_dir = tmp_path / task
    task_dir.mkdir()
    _write_binding_artifacts(task_dir)

    expected = set(diagnosis._completion_artifact_names(task))
    assert {
        "mini-swe-agent.trajectory.json",
        "brief_result.json",
        "gt_artifacts/gt_run_identity.json",
    } <= expected

    (task_dir / "brief_result.json").write_text('{"tampered":true}', encoding="utf-8")
    result = diagnosis._diagnosis_integrity(
        {task: (task_dir / f"gt_feature_metrics_{task}.json", {})},
        {"run_id": "run-1"}, inventory.canonical_feature_inventory()["CAP"],
    )
    assert result["publishable"] is False
    assert "task_completion:artifact_hash:brief_result.json" in result["tasks"][task]["issues"]


def test_diagnosis_integrity_requires_exact_gt_on_identity_and_member_sources(
    tmp_path: Path,
) -> None:
    task = "repo__task-1"
    task_dir = tmp_path / task
    task_dir.mkdir()
    _write_binding_artifacts(task_dir)
    artifacts = task_dir / "gt_artifacts"

    identity_path = artifacts / "gt_run_identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["gt_ref_requested"] = "release/ss-diagnosis"
    identity["gt_ref_prepared_sha"] = "e" * 40
    identity["baseline"] = True
    identity_path.write_text(json.dumps(identity), encoding="utf-8")

    receipt_path = artifacts / "gt_profile_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    member = next(iter(receipt["member_source"]))
    receipt["member_source"][member] = "fabricated"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    integrity = diagnosis._diagnosis_integrity(
        {task: (task_dir / f"gt_feature_metrics_{task}.json", {})},
        {"run_id": "run-1"},
        inventory.canonical_feature_inventory()["CAP"],
    )

    issues = integrity["tasks"][task]["issues"]
    assert "run_identity:gt_ref_binding" in issues
    assert "run_identity:baseline" in issues
    assert "profile_receipt:member_source" in issues


def test_diagnosis_integrity_accepts_symbolic_ref_bound_to_prepared_commit(
    tmp_path: Path,
) -> None:
    task = "repo__task-1"
    task_dir = tmp_path / task
    task_dir.mkdir()
    _write_binding_artifacts(task_dir)
    identity_path = task_dir / "gt_artifacts" / "gt_run_identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["gt_ref_requested"] = "release/ss-diagnosis-20260714"
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    _write_completion_receipt(task_dir)

    integrity = diagnosis._diagnosis_integrity(
        {task: (task_dir / f"gt_feature_metrics_{task}.json", {})},
        {"run_id": "run-1"},
        inventory.canonical_feature_inventory()["CAP"],
    )

    assert integrity["publishable"] is True
    assert integrity["tasks"][task]["status"] == "BOUND"
    assert integrity["identity_consensus"]["gt_ref_requested"] == (
        "release/ss-diagnosis-20260714"
    )
    assert integrity["identity_consensus"]["gt_ref_prepared_sha"] == "a" * 40


def test_diagnosis_integrity_rejects_malformed_ref_and_cross_task_disagreement(
    tmp_path: Path,
) -> None:
    tasks = {}
    for index in (1, 2):
        task = f"repo__task-{index}"
        task_dir = tmp_path / task
        task_dir.mkdir()
        _write_binding_artifacts(task_dir)
        identity_path = task_dir / "gt_artifacts" / "gt_run_identity.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["gt_ref_requested"] = f"release/ss-{index}"
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        tasks[task] = (task_dir / f"gt_feature_metrics_{task}.json", {})

    integrity = diagnosis._diagnosis_integrity(
        tasks, {"run_id": "run-1"}, inventory.canonical_feature_inventory()["CAP"]
    )
    assert integrity["publishable"] is False
    assert integrity["run_issues"] == ["run_identity_consensus"]

    first = tmp_path / "repo__task-1" / "gt_artifacts" / "gt_run_identity.json"
    identity = json.loads(first.read_text(encoding="utf-8"))
    identity["gt_ref_requested"] = "bad ref\n"
    first.write_text(json.dumps(identity), encoding="utf-8")
    malformed = diagnosis._diagnosis_integrity(
        {"repo__task-1": tasks["repo__task-1"]},
        {"run_id": "run-1"},
        inventory.canonical_feature_inventory()["CAP"],
    )
    assert "run_identity:gt_ref_binding" in malformed["tasks"]["repo__task-1"]["issues"]


def test_cli_writes_unpublishable_diagnosis_then_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnosis, "diagnose_run", lambda *_args: {
        "schema": "gt.ss_live_diagnosis.v1",
        "feature_count": 129,
        "integrity": {"publishable": False},
        "rows": [],
    })
    output = tmp_path / "diagnosis.json"

    assert diagnosis.main([
        str(tmp_path), "--run-metrics", str(tmp_path / "run.json"),
        "--output", str(output),
    ]) == 3
    assert json.loads(output.read_text(encoding="utf-8"))["integrity"] == {
        "publishable": False,
    }


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

    rows, entries, opportunity, error = diagnosis._task_artifacts(metrics_path, {})

    assert error is None
    assert rows == [row]
    assert len(entries) == 1
    assert entries[0]["join_method"] == "seal"
    assert entries[0]["feature_lineage"]["fact_class"] == "caller_contract"
    assert opportunity["features"]["caller_contract"]["status"] == "UNMEASURED"
