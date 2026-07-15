from __future__ import annotations

import copy
import hashlib
import json

from scripts.swebench.live_evidence import validate_live_evidence


RUN = "29427671636"
TASK = "org__repo-123"
PREPARED = "a" * 40
SUBSTRATE = "ghcr.io/example/gt-substrate@sha256:" + "b" * 64
SEAM = "c" * 64
PAYLOAD = "[GroundTruth] src/core.py: preserve parse_record contract"
SEAL = hashlib.sha256(PAYLOAD.encode()).hexdigest()[:16]


def _artifact_sha(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _valid_case() -> tuple[dict, dict, list[dict], dict, dict, dict[str, dict]]:
    identity = {
        "workflow_run_id": RUN,
        "task_id": TASK,
        "gt_ref_prepared_sha": PREPARED,
        "gt_ref_resolved": PREPARED,
        "substrate_digest_actual": SUBSTRATE,
        "seam_sha256": SEAM,
        "profile": "2",
        "baseline": False,
    }
    runtime_rows = [{
        "workflow_run_id": RUN,
        "task_id": TASK,
        "gt_ref_prepared_sha": PREPARED,
        "substrate_digest": SUBSTRATE,
        "seam_sha256": SEAM,
        "outcome": "delivered",
        "chars_delivered": len(PAYLOAD),
        "content_sha256_16": SEAL,
        "runtime_producer_id": "edit_check",
        "fact_class": "syntax_result",
    }]
    trajectory = {
        "workflow_run_id": RUN,
        "task_id": TASK,
        "seam_sha256": SEAM,
        "messages": [
            {"role": "tool", "content": "prefix\n" + PAYLOAD + "\nsuffix"},
            {"role": "assistant", "content": "I will preserve parse_record in src/core.py."},
        ],
    }
    consumption = {
        "schema": "gt.consumption_ledger.v2",
        "visible_audit_complete": True,
        "test_identity_leak_hits": [],
        "entries": [{
            "source": "trajectory",
            "joined": True,
            "join_method": "seal",
            "content_sha256_16": SEAL,
            "chars": len(PAYLOAD),
            "msg_index": 0,
            "physical_id": f"m0:7:{7 + len(PAYLOAD)}",
            "receipt": 2,
            "referenced_msg_index": 1,
            "acted_msg_index": None,
            "rendered_text": PAYLOAD,
            "feature_lineage": {
                "features": [
                    {"category": "CAP", "feature_id": "GT_EDIT_CHECK", "role": "byte_owner"},
                    {"category": "FACT", "feature_id": "syntax_result", "role": "fact"},
                ],
            },
        }],
    }
    nested = {
        "truth_source": {"kind": "producer_source", "value": "diagnostic payload"},
        "observed_state": {"kind": "repository_state", "revision": PREPARED},
        "research_boundary": {"kind": "chronological_boundary", "message": 1},
        "probe_assignment": {"kind": "shadow_assignment", "unit": TASK + ":edit_result"},
        "probe_treatment": {"kind": "treatment_observation", "message": 1},
        "probe_control": {"kind": "shadow_observation", "message": 1},
        "probe_outcome": {"kind": "measured_outcome", "metric": "acknowledged_relevant_action"},
    }
    nested_refs = {
        name: {"artifact_id": name, "sha256": _artifact_sha(value)}
        for name, value in nested.items()
    }
    truth = {
        "schema": "gt.producer_truth.v1",
        "evidence_source": "paid_live",
        "workflow_run_id": RUN,
        "task_id": TASK,
        "seam_sha256": SEAM,
        "delivery_seal": SEAL,
        "features": [
            {"feature_id": "GT_EDIT_CHECK", "family": "CAP", "role": "capability_support"},
            {"feature_id": "syntax_result", "family": "FACT", "role": "fact_delivery"},
        ],
        "producer_id": "edit_check",
        "truth_verdict": "TRUE",
        "freshness_verdict": "CURRENT",
        "source_evidence": nested_refs["truth_source"],
        "source_field": "diagnostic.normalized",
        "observed_state_evidence": nested_refs["observed_state"],
    }
    timing = {
        "schema": "gt.rl_timing.v1",
        "evidence_source": "paid_live",
        "workflow_run_id": RUN,
        "task_id": TASK,
        "seam_sha256": SEAM,
        "delivery_seal": SEAL,
        "features": [
            {"feature_id": "GT_EDIT_CHECK", "family": "CAP", "role": "capability_support"},
            {"feature_id": "syntax_result", "family": "FACT", "role": "fact_delivery"},
        ],
        "grader": "chronological_research_boundary",
        "adjudicator_id": "gt_math_chronological_v1",
        "verdict": "ON_TIME",
        "required_event": "edit_result",
        "actual_event": "edit_result",
        "home_message": 0,
        "decision_boundary_message": 1,
        "research_boundary_evidence": nested_refs["research_boundary"],
    }
    fair = {
        "schema": "gt.fair_probe.v1",
        "evidence_source": "paid_live",
        "workflow_run_id": RUN,
        "task_id": TASK,
        "seam_sha256": SEAM,
        "delivery_seal": SEAL,
        "features": [
            {"feature_id": "GT_EDIT_CHECK", "family": "CAP", "role": "capability_support"},
            {"feature_id": "syntax_result", "family": "FACT", "role": "fact_delivery"},
        ],
        "probe_id": "shadow-17",
        "design": "SHADOW",
        "adjudicator_id": "gt_fair_probe_v1",
        "causal_verdict": "GT_CAUSED_BEHAVIOR",
        "assignment_unit_id": TASK + ":edit_result",
        "assignment_evidence": nested_refs["probe_assignment"],
        "treatment_evidence": nested_refs["probe_treatment"],
        "control_evidence": nested_refs["probe_control"],
        "outcome_evidence": nested_refs["probe_outcome"],
        "outcome_metric": "acknowledged_relevant_action",
        "acknowledgment_message": 1,
    }
    artifacts = {**nested, "truth": truth, "timing": timing, "fair": fair}
    refs = {
        name: {"artifact_id": name, "sha256": _artifact_sha(value)}
        for name, value in artifacts.items()
    }
    contract = {
        "schema": "gt.ss_live_evidence.v1",
        "evidence_source": "paid_live",
        "run_identity": {
            "workflow_run_id": RUN,
            "task_id": TASK,
            "gt_ref_prepared_sha": PREPARED,
            "gt_ref_resolved": PREPARED,
            "substrate_digest": SUBSTRATE,
            "seam_sha256": SEAM,
            "profile": "2",
            "baseline": False,
        },
        "deliveries": [{
            "delivery_id": "delivery-0",
            "features": [
                {"feature_id": "GT_EDIT_CHECK", "family": "CAP", "role": "capability_support"},
                {"feature_id": "syntax_result", "family": "FACT", "role": "fact_delivery"},
            ],
            "physical_delivery": {
                "runtime_ledger_index": 0,
                "content_sha256_16": SEAL,
                "chars_delivered": len(PAYLOAD),
                "physical_id": f"m0:7:{7 + len(PAYLOAD)}",
                "home_message": 0,
            },
            "acknowledgment": {
                "receipt_level": 2,
                "message_index": 1,
                "channel": "prose",
                "anchor": "preserve parse_record",
            },
            "truth_evidence": refs["truth"],
            "timing_evidence": refs["timing"],
            "leak_count": 0,
            "dose_count": 1,
            "fair_probe_evidence": refs["fair"],
        }],
    }
    return contract, identity, runtime_rows, trajectory, consumption, artifacts


def _validate(case, *, parsed_evidence: bool = False) -> dict:
    contract, identity, rows, trajectory, consumption, artifacts = case
    artifact_bytes = {
        name: json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        for name, value in artifacts.items()
    }
    evidence_manifest = {
        "schema": "gt.live_evidence_artifacts.v1",
        "workflow_run_id": RUN,
        "task_id": TASK,
        "gt_ref_prepared_sha": PREPARED,
        "substrate_digest": SUBSTRATE,
        "seam_sha256": SEAM,
        "profile": "2",
        "baseline": False,
        "artifacts": {
            name: hashlib.sha256(raw).hexdigest()
            for name, raw in artifact_bytes.items()
        },
    }
    run_identity = {
        "schema": "gt.run_identity.v1",
        "workflow_run_id": identity["workflow_run_id"],
        "gt_ref_prepared_sha": identity["gt_ref_prepared_sha"],
        "gt_ref_resolved": identity["gt_ref_resolved"],
        "substrate_digest_actual": identity["substrate_digest_actual"],
        "seam_sha256": identity["seam_sha256"],
        "baseline": identity["baseline"],
    }
    completion_artifacts = {
        "gt_artifacts/gt_run_identity.json": json.dumps(
            run_identity, sort_keys=True, separators=(",", ":"),
        ).encode(),
        "gt_artifacts/gt_live_evidence_artifacts.json": json.dumps(
            evidence_manifest, sort_keys=True, separators=(",", ":"),
        ).encode(),
    }
    completion_receipt = {
        "schema": "gt.task_completion.v1",
        "task": TASK,
        "workflow_run_id": RUN,
        "status": "COMPLETE",
        "artifact_sha256": {
            path: hashlib.sha256(raw).hexdigest()
            for path, raw in completion_artifacts.items()
        },
    }
    return validate_live_evidence(
        contract,
        canonical_identity=identity,
        runtime_ledger_rows=rows,
        trajectory=trajectory,
        consumption_ledger=consumption,
        evidence_artifacts=artifacts if parsed_evidence else artifact_bytes,
        completion_receipt=json.dumps(
            completion_receipt, sort_keys=True, separators=(",", ":"),
        ).encode(),
        completion_artifacts=completion_artifacts,
    )


def test_accepts_exact_paid_live_seven_gate_contract_without_promoting() -> None:
    report = _validate(_valid_case())
    assert report == {
        "schema": "gt.ss_live_evidence.validation.v1",
        "valid": True,
        "errors": [],
        "validated_delivery_ids": ["delivery-0"],
        "proof_scope": "validation_only_no_ss_live_promotion",
    }


def test_rejects_summary_booleans_and_terminal_promotion_fields() -> None:
    case = _valid_case()
    case[0]["deliveries"][0]["correct_info"] = True
    case[0]["ss_live"] = True
    report = _validate(case)
    assert not report["valid"]
    assert "contract:unexpected_fields:ss_live" in report["errors"]
    assert "delivery-0:unexpected_fields:correct_info" in report["errors"]


def test_rejects_offline_replay_and_unknown_identity() -> None:
    case = _valid_case()
    case[0]["evidence_source"] = "replay"
    case[0]["run_identity"]["gt_ref_prepared_sha"] = ""
    report = _validate(case)
    assert "contract:evidence_source_not_paid_live" in report["errors"]
    assert "identity:gt_ref_prepared_sha" in report["errors"]


def test_rejects_cross_run_task_and_seal_artifact_joins() -> None:
    case = _valid_case()
    case[5]["truth"]["task_id"] = "other__task"
    case[5]["timing"]["workflow_run_id"] = "other-run"
    case[5]["fair"]["delivery_seal"] = "0" * 16
    for name in ("truth", "timing", "fair"):
        case[0]["deliveries"][0][f"{name if name != 'fair' else 'fair_probe'}_evidence"]["sha256"] = _artifact_sha(case[5][name])
    report = _validate(case)
    assert "delivery-0:truth_evidence:task_binding" in report["errors"]
    assert "delivery-0:timing_evidence:run_binding" in report["errors"]
    assert "delivery-0:fair_probe_evidence:seal_binding" in report["errors"]


def test_rejects_contract_task_not_bound_to_canonical_identity() -> None:
    case = _valid_case()
    case[1]["task_id"] = "other__task"
    report = _validate(case)
    assert "identity:canonical_mismatch:task_id" in report["errors"]


def test_rejects_hash_mismatch_and_missing_evidence_artifact() -> None:
    case = _valid_case()
    case[0]["deliveries"][0]["truth_evidence"]["sha256"] = "0" * 64
    del case[5]["timing"]
    report = _validate(case)
    assert "delivery-0:truth_evidence:completion_hash_mismatch" in report["errors"]
    assert "delivery-0:timing_evidence:not_completion_sealed" in report["errors"]


def test_rejects_parsed_artifact_map_without_exact_bytes() -> None:
    report = _validate(_valid_case(), parsed_evidence=True)
    assert not report["valid"]
    assert "delivery-0:truth_evidence:exact_bytes_required" in report["errors"]


def test_rejects_arbitrary_live_wrappers_without_completion_seal() -> None:
    contract, identity, rows, trajectory, consumption, artifacts = _valid_case()
    artifact_bytes = {
        name: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        for name, value in artifacts.items()
    }
    report = validate_live_evidence(
        contract,
        canonical_identity=identity,
        runtime_ledger_rows=rows,
        trajectory=trajectory,
        consumption_ledger=consumption,
        evidence_artifacts=artifact_bytes,
        completion_receipt=b"{}",
        completion_artifacts={},
    )
    assert not report["valid"]
    assert "completion:schema" in report["errors"]
    assert "completion:evidence_manifest:missing" in report["errors"]


def test_rejects_unopened_or_hash_mismatched_nested_evidence() -> None:
    case = _valid_case()
    del case[5]["truth_source"]
    case[5]["fair"]["control_evidence"]["sha256"] = "0" * 64
    case[0]["deliveries"][0]["fair_probe_evidence"]["sha256"] = _artifact_sha(
        case[5]["fair"]
    )
    report = _validate(case)
    assert "delivery-0:truth_evidence:source_evidence:not_completion_sealed" in report["errors"]
    assert "delivery-0:fair_probe_evidence:control_evidence:completion_hash_mismatch" in report["errors"]


def test_rejects_cross_ledger_join_and_unsealed_visible_entry() -> None:
    case = _valid_case()
    case[2][0]["content_sha256_16"] = "0" * 16
    case[4]["entries"][0]["join_method"] = "legacy_content"
    report = _validate(case)
    assert "delivery-0:runtime_ledger:seal_mismatch" in report["errors"]
    assert "delivery-0:consumption:exact_seal_join_required" in report["errors"]


def test_rejects_non_later_non_assistant_acknowledgment() -> None:
    case = _valid_case()
    case[0]["deliveries"][0]["acknowledgment"]["message_index"] = 0
    case[0]["deliveries"][0]["acknowledgment"]["anchor"] = "invented anchor"
    report = _validate(case)
    assert "delivery-0:acknowledgment:not_later" in report["errors"]
    assert "delivery-0:acknowledgment:not_assistant" in report["errors"]


def test_rejects_unanchored_assistant_acknowledgment() -> None:
    case = _valid_case()
    case[0]["deliveries"][0]["acknowledgment"]["anchor"] = "parse_record"
    case[3]["messages"][1]["content"] = "I will preserve the contract."
    report = _validate(case)
    assert "delivery-0:acknowledgment:anchor_not_found" in report["errors"]


def test_rejects_ack_anchor_that_is_only_a_substring_of_delivered_entity() -> None:
    case = _valid_case()
    case[0]["deliveries"][0]["acknowledgment"]["anchor"] = "parse"
    report = _validate(case)
    assert "delivery-0:acknowledgment:anchor_not_delivered_entity" in report["errors"]


def test_rejects_tool_output_as_acknowledgment() -> None:
    case = _valid_case()
    case[3]["messages"][1]["role"] = "tool"
    report = _validate(case)
    assert "delivery-0:acknowledgment:not_assistant" in report["errors"]


def test_rejects_duplicate_delivery_and_duplicate_typed_features() -> None:
    case = _valid_case()
    duplicate = copy.deepcopy(case[0]["deliveries"][0])
    duplicate["delivery_id"] = "delivery-1"
    case[0]["deliveries"].append(duplicate)
    case[0]["deliveries"][0]["features"].append(
        copy.deepcopy(case[0]["deliveries"][0]["features"][0])
    )
    report = _validate(case)
    assert "delivery-0:features:duplicate" in report["errors"]
    assert "delivery-1:physical_delivery:duplicate" in report["errors"]


def test_rejects_unknown_feature_or_wrong_typed_role() -> None:
    case = _valid_case()
    case[0]["deliveries"][0]["features"][0] = {
        "feature_id": "GT_NOT_REAL", "family": "CAP", "role": "capability_support"
    }
    case[0]["deliveries"][0]["features"][1]["role"] = "measurement"
    report = _validate(case)
    assert "delivery-0:features:unknown:GT_NOT_REAL" in report["errors"]
    assert "delivery-0:features:role_mismatch:syntax_result" in report["errors"]


def test_rejects_lineage_claim_not_owned_by_physical_delivery() -> None:
    case = _valid_case()
    case[4]["entries"][0]["feature_lineage"]["features"] = [
        {"category": "FACT", "feature_id": "syntax_result", "role": "fact"}
    ]
    report = _validate(case)
    assert "delivery-0:features:lineage_mismatch:GT_EDIT_CHECK" in report["errors"]


def test_rejects_wrong_truth_freshness_timing_or_fair_probe_verdicts() -> None:
    case = _valid_case()
    case[5]["truth"]["truth_verdict"] = "UNKNOWN"
    case[5]["truth"]["freshness_verdict"] = "STALE"
    case[5]["timing"]["verdict"] = "STEP_BEHIND"
    case[5]["fair"]["causal_verdict"] = "ASSOCIATED"
    for name in ("truth", "timing", "fair"):
        key = f"{name if name != 'fair' else 'fair_probe'}_evidence"
        case[0]["deliveries"][0][key]["sha256"] = _artifact_sha(case[5][name])
    report = _validate(case)
    assert "delivery-0:truth_evidence:truth_not_true" in report["errors"]
    assert "delivery-0:truth_evidence:freshness_not_current" in report["errors"]
    assert "delivery-0:timing_evidence:not_on_time" in report["errors"]
    assert "delivery-0:fair_probe_evidence:not_causal" in report["errors"]


def test_rejects_leak_and_stacked_dose_even_if_contract_claims_zero_and_one() -> None:
    case = _valid_case()
    leaked = PAYLOAD + " FAIL_TO_PASS"
    leaked_seal = hashlib.sha256(leaked.encode()).hexdigest()[:16]
    entry = case[0]["deliveries"][0]
    entry["physical_delivery"].update({
        "content_sha256_16": leaked_seal, "chars_delivered": len(leaked),
        "physical_id": f"m0:7:{7 + len(leaked)}",
    })
    case[2][0].update({"content_sha256_16": leaked_seal, "chars_delivered": len(leaked)})
    case[3]["messages"][0]["content"] = "prefix\n" + leaked + "\nsuffix"
    case[4]["entries"][0].update({
        "content_sha256_16": leaked_seal, "chars": len(leaked), "rendered_text": leaked,
        "physical_id": f"m0:7:{7 + len(leaked)}",
    })
    for name in ("truth", "timing", "fair"):
        case[5][name]["delivery_seal"] = leaked_seal
        key = f"{name if name != 'fair' else 'fair_probe'}_evidence"
        entry[key]["sha256"] = _artifact_sha(case[5][name])
    case[4]["entries"].append({
        **copy.deepcopy(case[4]["entries"][0]),
        "physical_id": "m0:0:6",
        "content_sha256_16": hashlib.sha256(b"prefix").hexdigest()[:16],
        "rendered_text": "prefix",
        "chars": 6,
    })
    report = _validate(case)
    assert "delivery-0:leak_count:observed_nonzero" in report["errors"]
    assert "delivery-0:dose_count:observed:2" in report["errors"]


def test_rejects_contract_leak_or_dose_claim_not_exact_integer() -> None:
    case = _valid_case()
    case[0]["deliveries"][0]["leak_count"] = False
    case[0]["deliveries"][0]["dose_count"] = 2
    report = _validate(case)
    assert "delivery-0:leak_count:must_be_zero_integer" in report["errors"]
    assert "delivery-0:dose_count:must_be_one_integer" in report["errors"]
