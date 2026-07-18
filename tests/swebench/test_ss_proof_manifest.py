from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.swebench import ss_proof_manifest as manifest


ROOT = Path(__file__).resolve().parents[2]
SCOREBOARD = ROOT / ".claude" / "reports" / "SS_SCOREBOARD.json"
TASKS_29 = [f"task-{index:02d}" for index in range(29)]
BASE_CONTEXT = {
    "run_id": "run-1", "task_id": "task-00", "seam_sha256": "a" * 64,
    "expected_replay_tasks": TASKS_29,
}


def _write_bytes(root: Path, relative: str, raw: bytes) -> dict[str, str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"path": relative, "sha256": hashlib.sha256(raw).hexdigest()}


def _write_json(root: Path, relative: str, value: object) -> dict[str, str]:
    return _write_bytes(root, relative, json.dumps(value, sort_keys=True).encode())


def _context(root: Path, **changes: object) -> dict:
    return {**BASE_CONTEXT, "proof_root": str(root), **changes}


def _gate_report(execution_id: str | None = None) -> dict:
    scenarios = [
        {
            "id": f"S{index}",
            "verdict": "HONEST-STRUCTURAL-SKIP" if index == 8 else "PASS",
        }
        for index in range(12)
    ]
    report = {
        "gate": "ss_gate", "exit_code": 0,
        "generated_utc": "20260714T010000Z", "scenarios": scenarios,
    }
    if execution_id is not None:
        report.update({
            "execution_id": execution_id, "run_id": BASE_CONTEXT["run_id"],
            "seam_sha256": BASE_CONTEXT["seam_sha256"],
        })
    return report


def _canonical_rows(proof_manifest: dict) -> dict[str, dict]:
    rows = {}
    for name, definition in proof_manifest["features"].items():
        item = {
            "family": definition["family"],
            "ss_readiness": {"role": definition["role"], "gates": {}},
        }
        if definition["family"] == "ACQ":
            item.update({"source": "acq_provenance"})
        if definition["family"] == "PERF":
            item.update({
                "status": "MEASURED", "source": "gt_deep_metrics",
                "metric_structure_valid": True, "artifact_schema_valid": True,
                "precision_decimals": 8, "formula_provenance": "MANDATORY_METRICS.md",
                "denominator_provenance": "mandatory_contract",
            })
        rows[name] = item
    return rows


def _run_metrics(proof_manifest: dict) -> dict:
    mandatory: dict[str, dict[str, dict]] = {}
    for name, definition in proof_manifest["features"].items():
        if definition["family"] != "PERF":
            continue
        section = definition["ownership"]["section"]
        mandatory.setdefault(section, {})[name] = {
            "value_type": definition["ownership"]["value_type"],
            "status": "MEASURED", "missing_tasks": [], "mean": 1.0, "median": 1.0,
        }
    return {
        "schema": "gt_run_metrics.v2", "run_id": BASE_CONTEXT["run_id"],
        "precision_decimals": 8, "mandatory_performance_metric_count": 58,
        "mandatory_performance_collection_complete": True,
        "mandatory_performance_collection_failures": [],
        "mandatory_performance_metrics_with_missing_tasks": [],
        "mandatory_performance": mandatory,
    }


def test_v3_manifest_is_exact_128_and_preserves_typed_roles() -> None:
    result = manifest.build_ss_proof_manifest(SCOREBOARD)
    assert result["schema"] == "gt.ss_proof_manifest.v3"
    assert result["family_counts"] == {"ACQ": 12, "CAP": 48, "FACT": 11, "PERF": 58}
    assert len(result["features"]) == 129
    assert result["features"]["graph_validity"]["role"] == "support"
    assert result["features"]["GT_HYPOTHESIS"]["role"] == "capability_support"
    assert result["features"]["GT_GATEWAY"]["role"] == "infra_control"
    assert result["features"]["syntax_result"]["role"] == "fact_delivery"
    assert result["features"]["cochange_prior"]["role"] == "internal_support"
    assert result["features"]["gold_rank"]["role"] == "measurement"


def test_cochange_fact_row_is_internal_support_not_fabricated_delivery() -> None:
    row = manifest.build_ss_proof_manifest(SCOREBOARD)["features"]["cochange_prior"]

    assert row["family"] == "FACT"
    assert row["role"] == "internal_support"
    assert row["terminal_contract"]["independent_delivery_gates"] is False
    assert row["terminal_contract"]["kind"] == "internal_support_control"
    assert not (
        set(row["live_proof_dependencies"])
        & set(manifest.DELIVERY_LIVE_PROOF_DEPENDENCIES)
    )
    assert row["chronological_boundary_authority"] is None
    assert row["receipt_rule"]["independent_receipt"] is False
    assert row["BLOCKED_BY"] == [
        "COCHANGE_INTERNAL_TRUTH_WITNESS_ABSENT",
        "COCHANGE_INTERNAL_CAUSAL_PROBE_ABSENT",
    ]
    assert "cochange_evidence" in row["truth_authority"]["missing_writer"]


def test_cap_terminal_contracts_distinguish_byte_owners_mediators_and_eligibility() -> None:
    """CAP proof shape comes from executable ownership, never direct/infra shorthand."""
    result = manifest.build_ss_proof_manifest(SCOREBOARD)
    rows = result["features"]
    # P4 (B-TERM 2026-07-16): GT_SS_COHERENCE_V2 reclassified byte_owner → mediator; it now grades
    # on the control-mediation terminal (see below), not the capability_delivery byte-owner bar.
    byte_owners = {
        "GT_EDIT_CHECK", "GT_CHANGE_SURFACE", "GT_PATCH_DELTA", "GT_HYPOTHESIS",
        "GT_LOC_RESLOT", "GT_SS_SUBMIT_RED", "GT_CERT_DELIVERY",
    }
    assert {
        name for name in result["features"]
        if rows[name]["family"] == "CAP"
        and rows[name]["ownership"].get("cap_role") == "byte_owner"
    } == byte_owners
    assert rows["GT_SS_COHERENCE_V2"]["ownership"]["cap_role"] == "mediator"
    assert rows["GT_SS_COHERENCE_V2"]["terminal_contract"]["kind"] == "control_mediation"
    assert rows["GT_SS_COHERENCE_V2"]["terminal_contract"]["independent_delivery_gates"] is False
    for name in byte_owners:
        assert rows[name]["terminal_contract"]["independent_delivery_gates"] is True
        assert rows[name]["terminal_contract"]["kind"] == "capability_delivery"
        assert rows[name]["live_proof_dependencies"] == list(
            manifest.CAP_BYTE_OWNER_LIVE_PROOF_DEPENDENCIES
        )
    assert rows["GT_GATEWAY"]["ownership"]["cap_role"] == "mediator"
    assert rows["GT_GATEWAY"]["terminal_contract"]["independent_delivery_gates"] is False
    assert rows["GT_GATEWAY"]["terminal_contract"]["kind"] == "control_mediation"
    assert rows["GT_GATEWAY"]["live_proof_dependencies"] == list(
        manifest.INFRA_LIVE_PROOF_DEPENDENCIES
    )
    assert rows["GT_SS_LATE_DROP"]["ownership"]["cap_role"] == "eligibility"
    assert rows["GT_SS_LATE_DROP"]["terminal_contract"]["independent_delivery_gates"] is False
    assert rows["GT_SS_LATE_DROP"]["terminal_contract"]["kind"] == "eligibility_control"
    assert rows["GT_SS_LATE_DROP"]["live_proof_dependencies"] == list(
        manifest.ELIGIBILITY_LIVE_PROOF_DEPENDENCIES
    )
    assert not (
        set(rows["GT_GATEWAY"]["live_proof_dependencies"])
        & set(manifest.DELIVERY_LIVE_PROOF_DEPENDENCIES)
    )
    assert not (
        set(rows["GT_SS_LATE_DROP"]["live_proof_dependencies"])
        & set(manifest.DELIVERY_LIVE_PROOF_DEPENDENCIES)
    )


def test_preflight_is_explicitly_audit_only_and_never_dispatch_ready(tmp_path: Path) -> None:
    proof_manifest = manifest.build_ss_proof_manifest(SCOREBOARD)
    blocked = sum(
        bool(row["BLOCKED_BY"]) for row in proof_manifest["features"].values()
    )
    report = manifest.preflight_ss_proof(
        proof_manifest, task_context=_context(tmp_path), canonical_artifacts={}
    )
    assert report["dispatch_ready_enabled"] is False
    assert report["proof_promotion_enabled"] is False
    assert report["dispatch_integration"] == "absent:no production caller"
    assert report["counts"]["unknown"] == 129 - blocked
    assert report["counts"]["blocked"] == blocked
    assert sum(value for key, value in report["counts"].items() if key.endswith("_ready")) == 0


def test_synthetic_wrapper_exploit_cannot_complete_any_feature(tmp_path: Path) -> None:
    proof_manifest = manifest.build_ss_proof_manifest(SCOREBOARD)
    blocked = sum(
        bool(row["BLOCKED_BY"]) for row in proof_manifest["features"].values()
    )
    wrapper = _write_json(tmp_path, "pass.json", {
        "schema": "anything", "verdict": "PASS", "ss_live": True,
        "features": {name: {"verdict": "PASS"} for name in proof_manifest["features"]},
    })
    report = manifest.preflight_ss_proof(
        proof_manifest, mode="postrun", task_context=_context(tmp_path),
        canonical_artifacts={"smoke_audit": wrapper},
        proof_receipts={name: wrapper for name in proof_manifest["features"]},
        feature_fixture_refs={name: wrapper for name in proof_manifest["features"]},
    )
    assert report["counts"]["incomplete"] == 129 - blocked
    assert report["counts"]["blocked"] == blocked
    assert sum(value for key, value in report["counts"].items() if key.endswith("_complete")) == 0
    assert report["audits"]["fixture_burden"]["valid"] is False
    assert report["audits"]["live_seven_gates"]["smoke_summary_input_ignored"] is True


def test_actual_gate_shape_fails_for_missing_producer_bindings_and_nonce(tmp_path: Path) -> None:
    references = [
        _write_json(tmp_path, f"gate-{index}.json", _gate_report()) for index in (1, 2)
    ]
    audit = manifest._audit_gate_pair(references, _context(tmp_path))
    assert audit["valid"] is False
    assert "gate_0_missing_run_or_seam_binding" in audit["errors"]
    assert "gate_0_missing_producer_execution_id" in audit["errors"]


def test_gate_copies_do_not_count_as_two_executions(tmp_path: Path) -> None:
    report = _gate_report("execution-1")
    references = [
        _write_json(tmp_path, f"copy-{index}.json", report) for index in (1, 2)
    ]
    audit = manifest._audit_gate_pair(references, _context(tmp_path))
    assert audit["valid"] is False
    assert "gate_execution_ids_not_unique" in audit["errors"]


def test_replay_derives_exact29_and_rejects_self_attested_feature_coverage(tmp_path: Path) -> None:
    replay = {
        "schema": "ss.replay_oracle_report.v2", "exit_code": 0,
        "tasks_reconstructed": TASKS_29,
        "fixpoint": {task: {"replayed": True, "strict": True} for task in TASKS_29},
        "cases": [{"task": TASKS_29[0], "verdict": "PASS"}],
        "invariants": [{"name": "off-flag fixpoint", "verdict": "PASS"}],
        "feature_coverage": {"gold_rank": "PASS"},
    }
    reference = _write_json(tmp_path, "replay.json", replay)
    audit = manifest._audit_replay(reference, _context(tmp_path))
    assert audit["task_count"] == 29
    assert audit["valid"] is False
    assert "replay_self_attested_feature_coverage_ignored" in audit["errors"]
    assert "replay_source_hashes_absent_or_malformed" in audit["errors"]
    assert "replay_missing_run_or_seam_binding" in audit["errors"]


def test_runtime_joins_exact_bytes_but_ignores_untrusted_truth_timing_flags(
    tmp_path: Path,
) -> None:
    payload = "syntax result for src/module.py"
    seal = hashlib.sha256(payload.encode()).hexdigest()[:16]
    binding = {
        "run_id": BASE_CONTEXT["run_id"], "task_id": BASE_CONTEXT["task_id"],
        "seam_sha256": BASE_CONTEXT["seam_sha256"],
    }
    trajectory = {
        **binding, "messages": [
            {"role": "user", "content": f"prefix\n{payload}\nsuffix"},
            {"role": "assistant", "content": "I will edit src/module.py now."},
        ],
    }
    rows = [
        {**binding, "outcome": "shadow_holdout", "chars_delivered": len(payload),
         "content_sha256_16": seal, "layer": "edit.syntax",
         "profile_member": "GT_GATEWAY"},
        {**binding, "outcome": "delivered", "chars_delivered": len(payload),
         "content_sha256_16": seal, "layer": "edit.syntax", "file_path": "src/module.py",
         "truth_valid": True, "freshness_valid": True, "timing_verdict": "ON_TIME"},
    ]
    ledger = _write_bytes(
        tmp_path, "ledger.jsonl", ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    )
    trajectory_ref = _write_json(tmp_path, "trajectory.json", trajectory)
    audit = manifest._audit_runtime(ledger, trajectory_ref, _context(tmp_path))
    assert audit["valid"] is True
    assert len(audit["deliveries"]) == 1
    delivery = audit["deliveries"][0]
    assert delivery["fact_class"] == "syntax_result"
    assert delivery["profile_member"] is None
    assert delivery["delivered_byte_proven"] is True
    assert delivery["correct_info"] is None
    assert delivery["correct_rl_adhered_time"] is None
    assert delivery["acknowledged"] is True
    assert delivery["fair_probe"] is None


def test_runtime_missing_any_run_task_seam_binding_fails(tmp_path: Path) -> None:
    payload = "payload"
    trajectory = {"messages": [{"role": "user", "content": payload}]}
    row = {
        "outcome": "delivered", "chars_delivered": len(payload),
        "content_sha256_16": hashlib.sha256(payload.encode()).hexdigest()[:16],
        "layer": "edit.syntax",
    }
    ledger = _write_bytes(tmp_path, "ledger.jsonl", (json.dumps(row) + "\n").encode())
    trajectory_ref = _write_json(tmp_path, "trajectory.json", trajectory)
    audit = manifest._audit_runtime(ledger, trajectory_ref, _context(tmp_path))
    assert audit["valid"] is False
    assert "trajectory_missing_run_task_seam_binding" in audit["errors"]
    assert "ledger_delivery_0_missing_run_task_seam_binding" in audit["errors"]


def test_feature_metrics_flags_acq_without_ablation_and_delivery_shaped_perf(tmp_path: Path) -> None:
    proof_manifest = manifest.build_ss_proof_manifest(SCOREBOARD)
    rows = _canonical_rows(proof_manifest)
    rows["gold_rank"]["ss_readiness"] = {
        "role": "measurement", "gates": {"delivered_byte_proven": False}
    }
    record = {
        "schema": "gt.feature_metrics.v1", "run_id": BASE_CONTEXT["run_id"],
        "task": BASE_CONTEXT["task_id"], "seam_sha256": BASE_CONTEXT["seam_sha256"],
        "ss_features": rows,
        "ss_integrity": {
            "inventory_complete": True, "required_inputs_complete": True, "publishable": True,
        },
    }
    reference = _write_json(tmp_path, "feature-metrics.json", record)
    audit = manifest._audit_feature_metrics(reference, _context(tmp_path), proof_manifest)
    assert audit["valid"] is False
    assert "feature_metrics_perf_delivery_shape:gold_rank" in audit["errors"]
    assert "feature_metrics_acq_ablation_absent:graph_validity" in audit["errors"]


def test_feature_metrics_accepts_right_censored_as_measurement_only(tmp_path: Path) -> None:
    proof_manifest = manifest.build_ss_proof_manifest(SCOREBOARD)
    rows = _canonical_rows(proof_manifest)
    rows["files_to_gold_view"].update({
        "status": "RIGHT_CENSORED",
        "value": None,
        "observation": {
            "state": "RIGHT_CENSORED", "event": "first_gold_view",
            "clock": "unique_files", "lower_bound": 3,
            "terminal_horizon": 3, "terminal_status": "Submitted",
        },
    })
    record = {
        "schema": "gt.feature_metrics.v1", "run_id": BASE_CONTEXT["run_id"],
        "task": BASE_CONTEXT["task_id"], "seam_sha256": BASE_CONTEXT["seam_sha256"],
        "ss_features": rows,
        "ss_integrity": {
            "inventory_complete": True, "required_inputs_complete": True, "publishable": True,
        },
    }
    reference = _write_json(tmp_path, "feature-metrics-censored.json", record)

    audit = manifest._audit_feature_metrics(reference, _context(tmp_path), proof_manifest)

    assert "feature_metrics_perf_contract_invalid:files_to_gold_view" not in audit["errors"]

    rows["files_to_gold_view"]["observation"]["terminal_status"] = "Crashed"
    bad_reference = _write_json(tmp_path, "feature-metrics-bad-censor.json", record)
    bad_audit = manifest._audit_feature_metrics(
        bad_reference, _context(tmp_path), proof_manifest
    )
    assert "feature_metrics_perf_contract_invalid:files_to_gold_view" in bad_audit["errors"]


def test_run_metrics_actual_contract_still_requires_seam_binding(tmp_path: Path) -> None:
    proof_manifest = manifest.build_ss_proof_manifest(SCOREBOARD)
    record = _run_metrics(proof_manifest)
    reference = _write_json(tmp_path, "run-metrics.json", record)
    audit = manifest._audit_run_metrics(reference, _context(tmp_path), proof_manifest)
    assert audit["valid"] is False
    assert "run_metrics_missing_run_or_seam_binding" in audit["errors"]
    record["seam_sha256"] = BASE_CONTEXT["seam_sha256"]
    reference = _write_json(tmp_path, "run-metrics-bound.json", record)
    assert manifest._audit_run_metrics(reference, _context(tmp_path), proof_manifest)["valid"]


@pytest.mark.parametrize("breakage", ["missing", "hash", "path_escape", "malformed"])
def test_artifact_reader_fails_closed(tmp_path: Path, breakage: str) -> None:
    reference = _write_json(tmp_path, "proof.json", {"gate": "actual"})
    if breakage == "missing":
        reference["path"] = "missing.json"
    elif breakage == "hash":
        reference["sha256"] = "0" * 64
    elif breakage == "path_escape":
        outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
        outside.write_text("{}", encoding="utf-8")
        reference = {"path": f"../{outside.name}", "sha256": hashlib.sha256(b"{}").hexdigest()}
    else:
        reference = _write_bytes(tmp_path, "bad.json", b"{bad-json")
    assert manifest._open_json(reference, _context(tmp_path)) is None


def test_missing_context_bindings_are_named_and_never_rounded_up(tmp_path: Path) -> None:
    proof_manifest = manifest.build_ss_proof_manifest(SCOREBOARD)
    blocked = sum(
        bool(row["BLOCKED_BY"]) for row in proof_manifest["features"].values()
    )
    report = manifest.preflight_ss_proof(
        proof_manifest, mode="postrun", task_context={"proof_root": str(tmp_path)}
    )
    assert report["counts"]["incomplete"] == 129 - blocked
    assert "missing_run_id" in report["audits"]["gate_x2"]["errors"]
    assert "missing_task_id" in report["audits"]["runtime"]["errors"]
    assert "missing_seam_sha256" in report["audits"]["feature_metrics"]["errors"]


def test_manifest_fails_loudly_on_scoreboard_drift(tmp_path: Path) -> None:
    scoreboard = json.loads(SCOREBOARD.read_text(encoding="utf-8"))
    scoreboard["feature_inventory"]["FACT"][0] = "invented_fact"
    path = tmp_path / "scoreboard.json"
    path.write_text(json.dumps(scoreboard), encoding="utf-8")
    with pytest.raises(ValueError, match="scoreboard FACT name/count drift"):
        manifest.build_ss_proof_manifest(path)
