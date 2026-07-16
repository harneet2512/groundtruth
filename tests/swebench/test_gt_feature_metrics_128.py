from __future__ import annotations

import json
import hashlib
import copy
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_feature_metrics as metrics  # noqa: E402
import gt_feature_inventory as inventory  # noqa: E402
import gt_run_metrics  # noqa: E402
from artifact_deepswe import ledger_attestation  # noqa: E402
from groundtruth.runtime import fact_registry, rl_profile  # noqa: E402
from groundtruth.runtime.attestation_store import persist_attestation  # noqa: E402
from groundtruth.runtime.producer_attestation import (  # noqa: E402
    ATTESTATION_SCHEMA,
    FRESHNESS,
    PASS,
    ArtifactRef,
    DecisionBinding,
    PredicateAttestation,
    ProducerAttestation,
    ProofRef,
)
from groundtruth.pretask.v1r_brief import (  # noqa: E402
    _brief_block_receipts,
    _reduce_brief_to_minimal,
)


def _write_task(task_dir: Path, task: str, *, deep_metrics: dict | None = None) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({
            "messages": [{"role": "user", "content": "fixture task"}],
            "info": {"submission": ""},
            "trajectory_format": "mini-swe-agent",
        }),
        encoding="utf-8",
    )
    ledger = task_dir / f"gt_runtime_ledger_{task}.jsonl"
    ledger.write_text(
        json.dumps({
            "layer": "fixture", "event_type": "fixture", "file_path": "",
            "outcome": "shadow_holdout", "reason": "fixture",
            "chars_delivered": 0, "iteration": 0,
        }) + "\n",
        encoding="utf-8",
    )
    ledger_attestation.write_attestation(
        ledger,
        task_dir / f"gt_runtime_ledger_attestation_{task}.json",
        write_failures=0,
    )
    if deep_metrics is not None:
        (task_dir / f"gt_deep_metrics_{task}.json").write_text(
            json.dumps(deep_metrics), encoding="utf-8"
        )


def _complete_deep_metrics(task: str) -> dict:
    record: dict = {
        "task_id": task,
        "schema": "gt_deep_metrics.v2",
        "precision_decimals": 8,
        "performance": {"schema": "gt_performance_metrics.v1"},
        "behavioral_impact": {},
        "metric_applicability": {},
    }
    for section, definitions in inventory.performance_metric_definitions().items():
        target = (
            record["behavioral_impact"]
            if section == "behavioral_impact"
            else record["performance"].setdefault(section, {})
        )
        for name, value_type in definitions:
            if value_type == "run_ratio":
                target[name] = None
            elif value_type == "bool":
                target[name] = False
            elif value_type == "bool_per_file":
                target[name] = {"src/example.py": True}
            elif value_type == "per_tag_rate_dict":
                target[name] = {}
                record["metric_applicability"].setdefault(section, {})[name] = {
                    "applicable": False,
                    "predicate": "fixture_gt_delivery_present",
                    "reason": "fixture has no GT deliveries",
                }
            else:
                target[name] = 0
    record["performance"]["token_efficiency"]["cost_per_resolved_scope"] = "run_aggregate"
    record["performance"]["metric_applicability"] = {
        section: dict(contracts)
        for section, contracts in record["metric_applicability"].items()
        if section != "behavioral_impact"
    }
    return record


def test_canonical_inventory_is_dynamic_exact_128() -> None:
    families = inventory.canonical_feature_inventory()
    scoreboard = json.loads(
        (ROOT / ".claude" / "reports" / "SS_SCOREBOARD.json").read_text(encoding="utf-8")
    )["feature_inventory"]

    assert {family: len(names) for family, names in families.items()} == {
        "ACQ": 12,
        "CAP": 47,
        "FACT": 11,
        "PERF": 58,
    }
    assert families["CAP"] == tuple(sorted(rl_profile.PROFILE_MEMBERS["2"]))
    assert families["FACT"] == tuple(fact_registry.all_fact_classes())
    assert families["ACQ"] == tuple(scoreboard["ACQ"])
    assert families["PERF"] == tuple(
        name
        for definitions in inventory.performance_metric_definitions().values()
        for name, _ in definitions
    )
    flattened = [name for names in families.values() for name in names]
    assert len(flattened) == len(set(flattened)) == 128


def test_inventory_fails_loud_on_family_count_or_name_collision(monkeypatch) -> None:
    monkeypatch.setattr(inventory, "ACQ_FEATURES", inventory.ACQ_FEATURES[:-1])
    with pytest.raises(ValueError, match="ACQ.*12"):
        inventory.canonical_feature_inventory()

    monkeypatch.setattr(
        inventory,
        "ACQ_FEATURES",
        inventory.ACQ_FEATURES + ("GT_GATEWAY",),
    )
    with pytest.raises(ValueError, match="ACQ.*12|duplicate"):
        inventory.canonical_feature_inventory()


def test_collect_task_adds_128_master_without_changing_legacy_features(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "synthetic__complete"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))
    monkeypatch.setattr(
        metrics,
        "_acquisition_feature_records",
        lambda _task_dir, _ledger, _trajectory: (
            {
                name: {
                    "family": "ACQ", "status": "MEASURED",
                    "source": "acq_provenance", "source_artifact": "brief_result.json",
                }
                for name in inventory.ACQ_FEATURES
            },
            [],
        ),
    )

    record = metrics.collect_task(task, str(tmp_path), profile="2")

    assert set(record["features"]) == set(rl_profile.PROFILE_MEMBERS["2"])
    assert set(record["fact_classes"]) == set(fact_registry.all_fact_classes())
    assert len(record["features"]) == 47
    assert len(record["ss_features"]) == 128
    assert record["ss_feature_inventory_schema"] == "gt.ss_feature_inventory.v1"
    assert record["ss_integrity"]["family_counts"] == {
        "ACQ": 12,
        "CAP": 47,
        "FACT": 11,
        "PERF": 58,
    }
    assert record["ss_integrity"]["inventory_complete"] is True
    assert record["ss_integrity"]["required_inputs_complete"] is True
    assert record["ss_features"]["GT_GATEWAY"]["source"] == "features"
    assert record["ss_features"]["caller_contract"]["source"] == "fact_classes"
    assert record["ss_features"]["gold_rank"]["status"] == "MEASURED"
    assert record["ss_features"]["cost_per_resolved"]["status"] == "NOT_APPLICABLE"
    assert all(
        {"gates", "live_witness", "ss_live", "blockers"}
        <= set(feature["ss_readiness"])
        for feature in record["ss_features"].values()
    )
    assert all(
        feature["ss_readiness"]["ss_live"] is False
        for feature in record["ss_features"].values()
    )


def test_missing_deep_metrics_is_explicit_and_fails_canonical_integrity(tmp_path: Path) -> None:
    task = "synthetic__missing"
    _write_task(tmp_path, task)

    record = metrics.collect_task(task, str(tmp_path), profile="2")

    assert len(record["ss_features"]) == 128
    assert record["ss_features"]["gold_rank"]["status"] == "UNMEASURED"
    assert record["ss_features"]["gold_rank"]["value"] is None
    assert all(
        record["ss_features"][name]["status"] == "UNMEASURED"
        for name in inventory.ACQ_FEATURES
    )
    assert record["ss_integrity"]["required_inputs_complete"] is False
    assert "deep_metrics" in record["ss_integrity"]["missing_required_inputs"]

    aggregate = metrics.aggregate_run("run-missing", [record], profile="2")
    assert aggregate["ss_integrity"]["publishable"] is False
    assert aggregate["integrity"]["publishable"] is False
    assert aggregate["ss_integrity"]["tasks_with_incomplete_inputs"] == [task]


def test_expected_population_preserves_zero_record_aggregate_artifacts(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps({"task_ids": ["repo__pre-agent-failure"]}), encoding="utf-8")
    out = tmp_path / "out"

    assert metrics.main([
        str(tmp_path), "--run-id", "run-zero", "--out", str(out),
        "--expected-tasks-file", str(expected),
    ]) == 3

    run = json.loads((out / "gt_run_metrics_run-zero.json").read_text(encoding="utf-8"))
    integrity = json.loads(
        (out / "gt_metric_integrity_run-zero.json").read_text(encoding="utf-8")
    )
    assert run["n_tasks"] == 1
    assert run["observed_task_count"] == 0
    assert integrity["missing_task_records"] == ["repo__pre-agent-failure"]
    assert integrity["publishable"] is False


def test_implicit_empty_population_remains_a_nonpublishable_legacy_aggregate() -> None:
    aggregate = metrics.aggregate_run("run-empty", [], profile="2")

    assert aggregate["run_metrics"]["n_tasks"] == 0
    assert aggregate["integrity"]["publishable"] is False
    assert aggregate["ss_integrity"]["publishable"] is False


@pytest.mark.parametrize("ledger_text", ["", "not-json\n"])
def test_visible_audit_input_missingness_fails_closed(
    tmp_path: Path, ledger_text: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "synthetic__visible-audit"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))
    (tmp_path / f"gt_runtime_ledger_{task}.jsonl").write_text(
        ledger_text, encoding="utf-8"
    )
    monkeypatch.setattr(
        metrics,
        "_acquisition_feature_records",
        lambda _task_dir, _ledger, _trajectory: (
            {
                name: {"family": "ACQ", "status": "MEASURED"}
                for name in inventory.ACQ_FEATURES
            },
            [],
        ),
    )

    record = metrics.collect_task(task, str(tmp_path), profile="2")

    assert record["ss_integrity"]["visible_audit_complete"] is False
    assert record["ss_integrity"]["required_inputs_complete"] is False
    assert "visible_audit" in record["ss_integrity"]["missing_required_inputs"]


def test_visible_audit_rejects_arbitrary_json_object_even_with_forged_attestation(
    tmp_path: Path,
) -> None:
    trajectory = tmp_path / "trajectory.json"
    trajectory.write_text(
        json.dumps({"messages": [{"role": "user", "content": "x"}]}),
        encoding="utf-8",
    )
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    ledger.write_text('{"anything":"goes"}\n', encoding="utf-8")
    attestation = tmp_path / "gt_runtime_ledger_attestation_task.json"
    raw = ledger.read_bytes()
    attestation.write_text(json.dumps({
        "schema": "gt.runtime_ledger_attestation.v1",
        "ledger_filename": ledger.name,
        "row_count": 1,
        "byte_count": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "final_newline": True,
        "write_failures": 0,
    }), encoding="utf-8")

    assert metrics._visible_audit_inputs_complete(str(trajectory), str(ledger)) is False


def test_visible_audit_rejects_ledger_changed_after_terminal_attestation(
    tmp_path: Path,
) -> None:
    trajectory = tmp_path / "trajectory.json"
    trajectory.write_text(
        json.dumps({"messages": [{"role": "user", "content": "x"}]}),
        encoding="utf-8",
    )
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    ledger.write_text(json.dumps({
        "layer": "fixture", "event_type": "fixture", "file_path": "",
        "outcome": "eligible", "reason": "fixture", "chars_delivered": 0,
        "iteration": 0,
    }) + "\n", encoding="utf-8")
    ledger_attestation.write_attestation(
        ledger, tmp_path / "gt_runtime_ledger_attestation_task.json",
        write_failures=0,
    )
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "layer": "late", "event_type": "late", "file_path": "",
            "outcome": "eligible", "reason": "after-terminal",
            "chars_delivered": 0, "iteration": 1,
        }) + "\n")

    assert metrics._visible_audit_inputs_complete(str(trajectory), str(ledger)) is False


def test_collect_task_wires_real_brief_receipt_into_acq_master(tmp_path: Path) -> None:
    task = "synthetic__acq"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))
    full_brief = "\n".join([
        "<gt-task-brief>",
        '<gt-localization confidence="HIGH">',
        "src/pkg/loader.py",
        "</gt-localization>",
        "Requirements to satisfy (from the issue):",
        "- [ ] preserve loader behavior",
        "",
        "1. src/pkg/loader.py",
        "    Signature: load(config)",
        "    Callers: run",
        "</gt-task-brief>",
    ])
    # Match the production GT_BRIEF_NATIVE + GT_BRIEF_MINIMAL surface: native
    # obligations remain, while each ranked file is reduced to its header.
    brief = _reduce_brief_to_minimal(full_brief)
    block_receipts = _brief_block_receipts(brief)

    def sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    (tmp_path / "brief_result.json").write_text(json.dumps({
        "schema": "gt.brief_result.v1",
        "brief_text": brief,
        "brief_sha256": sha(brief),
        "metrics": {
            "graph_edge_count": 1,
            "structural_signal_count": 1,
            "fts5_signal_count": 1,
            "semantic_signal_count": 0,
            "localization_proof": [{
                "rank": 1,
                "path": "src/pkg/loader.py",
                "witness": "load called by run [CALLS]",
                "witness_verified": True,
                "components": {"reach": 0.5, "lex": 0.8, "sem": 0.0},
            }],
            "block_receipts": block_receipts,
        },
    }), encoding="utf-8")
    (tmp_path / f"gt_runtime_ledger_{task}.jsonl").write_text(json.dumps({
        "layer": "brief.task",
        "event_type": "task_start",
        "file_path": "",
        "outcome": "delivered",
        "reason": "step0_brief_prepend",
        "chars_delivered": len(brief),
        "content_sha256_16": sha(brief)[:16],
        "iteration": 0,
    }) + "\n", encoding="utf-8")
    ledger_attestation.write_attestation(
        tmp_path / f"gt_runtime_ledger_{task}.jsonl",
        tmp_path / f"gt_runtime_ledger_attestation_{task}.json",
        write_failures=0,
    )
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(json.dumps({
        "messages": [
            {"role": "user", "content": brief + "\n\nFix the issue."},
            {"role": "assistant", "content": "I will inspect src/pkg/loader.py."},
        ],
        "info": {"submission": ""},
    }), encoding="utf-8")

    record = metrics.collect_task(task, str(tmp_path), profile="2")

    assert "<gt-localization" not in brief
    assert "Requirements to satisfy (from the issue):" in brief
    assert "Signature:" not in brief
    assert record["ss_features"]["graph_validity"]["status"] == "MEASURED"
    assert record["ss_features"]["structural_depth"]["status"] == "MEASURED"
    assert record["ss_features"]["lexical_FTS5"]["receipt_level"] == 2
    assert record["ss_features"]["semantic_embedder"]["status"] == "UNMEASURED"
    acq_readiness = record["ss_features"]["graph_validity"]["ss_readiness"]
    assert acq_readiness["role"] == "support"
    assert acq_readiness["gates"] == {
        "supported_fact_delivery_join": True,
        "candidate_local_contribution": True,
        "source_contribution_correct": None,
        "timing_inherited_from_fact_delivery": None,
        "source_causal_fair_probe": None,
    }
    assert acq_readiness["support_complete"] is False
    assert acq_readiness["ss_live"] is False
    assert record["ss_integrity"]["required_inputs_complete"] is True


def test_cap_readiness_never_inherits_an_unattributed_fact_delivery(tmp_path: Path) -> None:
    task = "synthetic__cap-attribution"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))
    payload = "src/pkg.py:12 preserve callers"
    (tmp_path / f"gt_runtime_ledger_{task}.jsonl").write_text(json.dumps({
        "layer": "l3.contract",
        "event_type": "file_view",
        "outcome": "delivered",
        "chars_delivered": len(payload),
        "content_sha256_16": hashlib.sha256(payload.encode()).hexdigest()[:16],
        "file_path": "src/pkg.py",
    }) + "\n", encoding="utf-8")
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(json.dumps({
        "messages": [{"role": "tool", "content": payload}],
        "info": {"submission": ""},
    }), encoding="utf-8")

    record = metrics.collect_task(task, str(tmp_path), profile="2")

    assert record["fact_classes"]["caller_contract"]["delivered"]["value"] is True
    gateway_readiness = record["features"]["GT_GATEWAY"]["ss_readiness"]
    contract_readiness = record["features"]["GT_CONTRACT_NATIVE"]["ss_readiness"]
    assert gateway_readiness["role"] == "infra_control"
    assert contract_readiness["role"] == "infra_control"
    assert "delivered_byte_proven" not in gateway_readiness["gates"]
    assert "delivered_byte_proven" not in contract_readiness["gates"]
    assert contract_readiness["mediation"]["delivered_fact_ids"] == [
        "caller_contract"
    ]
    assert record["ss_features"]["GT_CONTRACT_NATIVE"]["ss_readiness"] == record[
        "features"
    ]["GT_CONTRACT_NATIVE"]["ss_readiness"]
    fact_readiness = record["ss_features"]["caller_contract"]["ss_readiness"]
    assert fact_readiness["gates"]["delivered_byte_proven"] is False
    assert fact_readiness["gates"]["correct_info"] is None
    assert fact_readiness["gates"]["fair_probe"] is None
    assert fact_readiness["ss_live"] is False


def test_perf_readiness_is_typed_measurement_not_delivery_gates(tmp_path: Path) -> None:
    task = "synthetic__perf-readiness"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))

    record = metrics.collect_task(task, str(tmp_path), profile="2")
    readiness = record["ss_features"]["gold_rank"]["ss_readiness"]

    assert readiness["role"] == "measurement"
    assert tuple(readiness["gates"]) == metrics._MEASUREMENT_GATE_NAMES
    assert readiness["gates"] == {
        "artifact_valid": True,
        "metric_structure_valid": True,
        "precision_8dp": True,
        "formula_provenance": True,
        "denominator_provenance": True,
        "applicability_resolved": True,
        "task_coverage": True,
        "aggregate_coverage": False,
    }
    assert readiness["measurement_complete"] is False
    assert readiness["live_witness"] is False
    assert readiness["ss_live"] is False
    assert readiness["blockers"] == ["aggregate_coverage", "live_witness"]


def test_perf_readiness_fails_closed_on_schema_precision_or_structure(tmp_path: Path) -> None:
    task = "synthetic__perf-malformed"
    deep = _complete_deep_metrics(task)
    deep["schema"] = "stale.metrics.v1"
    deep["precision_decimals"] = 4
    deep["behavioral_impact"]["per_tag_impact"] = {
        "gt-contract": {"total": 0, "pivots": 1}
    }
    _write_task(tmp_path, task, deep_metrics=deep)

    record = metrics.collect_task(task, str(tmp_path), profile="2")
    readiness = record["ss_features"]["per_tag_impact"]["ss_readiness"]

    assert record["ss_features"]["per_tag_impact"]["status"] == "UNMEASURED"
    assert readiness["gates"]["artifact_valid"] is False
    assert readiness["gates"]["metric_structure_valid"] is False
    assert readiness["gates"]["precision_8dp"] is False
    assert readiness["measurement_complete"] is False
    assert metrics._value_honors_8dp(0.12345678) is True
    assert metrics._value_honors_8dp(0.123456789) is False


def test_per_tag_impact_accepts_canonical_count_and_rate_mapping(tmp_path: Path) -> None:
    task = "synthetic__perf-per-tag-canonical"
    deep = _complete_deep_metrics(task)
    deep["behavioral_impact"]["per_tag_impact"] = {
        "recovery": {"total": 2, "pivots": 1, "rate": 0.5},
        "syntax_result": {"total": 3, "pivots": 2, "rate": 0.66666667},
    }
    deep["metric_applicability"]["behavioral_impact"]["per_tag_impact"] = {
        "applicable": True,
        "predicate": "total_deliveries > 0",
        "reason": "byte-joined deliveries provide per-tag denominators",
    }
    _write_task(tmp_path, task, deep_metrics=deep)

    record = metrics.collect_task(task, str(tmp_path), profile="2")
    feature = record["ss_features"]["per_tag_impact"]

    assert feature["status"] == "MEASURED"
    assert feature["value"] == deep["behavioral_impact"]["per_tag_impact"]
    assert feature["metric_structure_valid"] is True
    assert feature["value_precision_valid"] is True
    assert "PERF.per_tag_impact" not in record["ss_integrity"][
        "missing_feature_inputs"
    ]


def test_infra_cap_emits_mediation_links_without_inheriting_fact_delivery() -> None:
    fact = metrics.new_lifecycle("fixture")
    fact["eligible"] = metrics.measured(True, source_artifact="ledger")
    fact["produced"] = metrics.measured(True, source_artifact="ledger")
    fact["delivered"] = metrics.measured(True, source_artifact="ledger")
    fact["native_valid"] = metrics.measured(True, source_artifact="trajectory")
    fact["expired_late"] = metrics.measured(True, source_artifact="ledger")
    fact["stale"] = metrics.measured(True, source_artifact="ledger")
    fact["receipt_level"] = metrics.measured(3, source_artifact="trajectory")

    record = metrics.member_record(
        "GT_CONTRACT_NATIVE",
        {name: copy.deepcopy(fact) for name in fact_registry.all_fact_classes()},
        {
            "l6_staged": False, "l6_reindex": 0, "arbiter_candidates": 0,
            "arbiter_lost": 0, "dose_suppressed": 0,
            "any_produced": True, "any_delivered": True,
        },
        metrics.BASELINE_UNAVAILABLE,
        ledger_artifact="ledger.jsonl",
        traj_artifact="trajectory.json",
    )

    lifecycle = record["lifecycle"]
    for field in (
        "delivered", "native_valid", "expired_late", "stale", "receipt_level",
    ):
        assert lifecycle[field]["status"] == "NOT_ELIGIBLE"
        assert lifecycle[field]["value"] is None
    readiness = metrics._infra_control_readiness(
        "GT_CONTRACT_NATIVE",
        ("caller_contract",),
        {name: copy.deepcopy(fact) for name in fact_registry.all_fact_classes()},
        ledger_artifact="ledger.jsonl",
    )
    assert readiness["role"] == "infra_control"
    assert readiness["gates"]["runtime_member_control_receipt"] is None
    assert readiness["mediation"] == {
        "status": "UNMEASURED",
        "linked_fact_ids": ["caller_contract"],
        "runtime_linked_fact_ids": [],
        "runtime_linkage_reason": "exact profile-member control receipt unavailable",
        "eligible_fact_ids": ["caller_contract"],
        "produced_fact_ids": ["caller_contract"],
        "delivered_fact_ids": ["caller_contract"],
        "source_artifact": "ledger.jsonl",
    }


def test_fact_byte_proof_requires_exact_class_and_seal_join() -> None:
    row = {
        "layer": "l3.contract",
        "outcome": "delivered",
        "chars_delivered": 12,
        "content_sha256_16": "b" * 16,
        "lineage_schema": "gt.feature_lineage.v1",
        "evidence_type": "caller_contract",
        "runtime_producer_id": "contract_map",
        "registered_producer_id": "contract_map",
        "producer_registration_match": True,
        "fact_class": "caller_contract",
    }
    entry = {
        "source": "trajectory",
        "joined": True,
        "join_method": "seal",
        "content_sha256_16": "b" * 16,
        "chars": 12,
        "ledger_layer": "l3.contract",
    }

    assert metrics._fact_delivery_byte_proven(
        "caller_contract", [row], {"entries": [entry]}
    ) is True
    assert metrics._fact_delivery_byte_proven(
        "localization", [row], {"entries": [entry]}
    ) is False
    assert metrics._fact_delivery_byte_proven(
        "caller_contract", [row],
        {"entries": [{**entry, "join_method": "text_containment"}]},
    ) is False


def test_acq_readiness_needs_the_bounded_measured_provenance_status() -> None:
    forged = {
        "status": "UNMEASURED",
        "receipt_level": 3,
        "content_sha256_16": "c" * 16,
    }

    readiness = metrics._acquisition_readiness(
        forged, leak_free=True, dose_ok=True
    )

    assert readiness["role"] == "support"
    assert readiness["gates"]["supported_fact_delivery_join"] is False
    assert readiness["gates"]["candidate_local_contribution"] is False
    assert readiness["gates"]["source_causal_fair_probe"] is None
    assert readiness["ss_live"] is False


def test_inventory_integrity_rejects_a_malformed_readiness_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "synthetic__bad-readiness"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))
    monkeypatch.setattr(metrics, "_measurement_only_readiness", lambda *_args, **_kwargs: {})

    record = metrics.collect_task(task, str(tmp_path), profile="2")

    assert record["ss_integrity"]["inventory_complete"] is False


def test_missing_visible_byte_audit_keeps_leak_and_dose_unknown(tmp_path: Path) -> None:
    task_dir = tmp_path / "absent-audit"
    task_dir.mkdir()

    record = metrics.collect_task("synthetic__absent-audit", str(task_dir), profile="2")

    fact_gates = record["ss_features"]["caller_contract"]["ss_readiness"]["gates"]
    assert fact_gates["leak_zero"] is None
    assert fact_gates["dose_lte_one"] is None
    acq = record["ss_features"]["graph_validity"]["ss_readiness"]
    assert acq["role"] == "support"
    assert acq["gates"]["supported_fact_delivery_join"] is False
    assert acq["gates"]["source_causal_fair_probe"] is None
    control = record["ss_features"]["GT_GATEWAY"]["ss_readiness"]
    assert control["role"] == "infra_control"
    assert control["gates"]["runtime_member_control_receipt"] is None


def test_cap_byte_proof_requires_exact_member_and_producer_seal_join() -> None:
    row = {
        "profile_member": "GT_EDIT_CHECK",
        "layer": "edit.syntax",
        "outcome": "delivered",
        "chars_delivered": 12,
        "content_sha256_16": "a" * 16,
    }
    entry = {
        "source": "trajectory",
        "joined": True,
        "join_method": "seal",
        "content_sha256_16": "a" * 16,
        "chars": 12,
        "ledger_layer": "edit.syntax",
    }

    assert metrics._member_delivery_byte_proven(
        "GT_EDIT_CHECK", [row], {"entries": [entry]}
    ) is True
    assert metrics._member_delivery_byte_proven(
        "GT_GATEWAY", [row], {"entries": [entry]}
    ) is False
    assert metrics._member_delivery_byte_proven(
        "GT_EDIT_CHECK", [row], {"entries": [{**entry, "joined": False}]}
    ) is False
    assert metrics._member_delivery_byte_proven(
        "GT_EDIT_CHECK", [row],
        {"entries": [{**entry, "join_method": "text_containment"}]},
    ) is False


def test_legacy_projection_excluding_additive_readiness_is_byte_identical(
    tmp_path: Path,
) -> None:
    task = "synthetic__empty"
    _write_task(tmp_path, task)
    # Preserve the historical empty-input fixture for the byte-off contract.
    # Visible-audit completeness is additive SS integrity and must not alter
    # any legacy projection bytes for the same stored inputs.
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({
            "messages": [], "info": {"submission": ""},
            "trajectory_format": "mini-swe-agent",
        }),
        encoding="utf-8",
    )
    (tmp_path / f"gt_runtime_ledger_{task}.jsonl").write_text("", encoding="utf-8")
    (tmp_path / f"gt_runtime_ledger_{task}.jsonl").rename(
        tmp_path / "gt_runtime_ledger_synthetic.jsonl"
    )
    record = metrics.collect_task(task, str(tmp_path), profile="2")
    legacy_keys = (
        "schema", "grader_version", "profile", "task", "attempt", "artifacts",
        "features", "fact_classes", "atomic_events", "behavioural_endpoints", "integrity",
    )
    projection = {key: copy.deepcopy(record[key]) for key in legacy_keys}
    # ss_readiness is the additive SS extension under review. The byte-off
    # contract covers every pre-extension legacy field, not a superseded bug
    # inside the new projection itself.
    for feature in projection["features"].values():
        feature.pop("ss_readiness", None)
    encoded = json.dumps(
        projection, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")

    # Intentional authority correction: coherence retains its own CAP lifecycle
    # without serializing a fabricated recovery FACT projection.
    assert hashlib.sha256(encoded).hexdigest() == (
        "a604c4a7f05e5659e10a3d278cccc161438aa5947526ed33c8f328424eb972bf"
    )


def test_coherence_exact_profile_row_does_not_invent_atomic_fact_identity() -> None:
    row = {
        "layer": "detect.coherence",
        "profile_member": "GT_SS_COHERENCE_V2",
    }
    assert metrics._atomic_row_identity(row) is None


def test_present_but_incomplete_deep_metrics_fails_closed_per_metric(tmp_path: Path) -> None:
    task = "synthetic__partial"
    _write_task(tmp_path, task, deep_metrics={"task_id": task, "performance": {}})

    record = metrics.collect_task(task, str(tmp_path), profile="2")

    assert record["ss_features"]["gold_rank"]["status"] == "UNMEASURED"
    assert "PERF.gold_rank" in record["ss_integrity"]["missing_feature_inputs"]
    assert record["ss_integrity"]["required_inputs_complete"] is False


@pytest.mark.parametrize(
    ("section", "name", "raw"),
    [
        ("localization", "gold_rank", None),
        ("edit_quality", "first_edit_correctness", {}),
        ("behavioral_impact", "per_tag_impact", {}),
    ],
)
def test_task_perf_null_or_empty_needs_explicit_non_applicability(
    tmp_path: Path, section: str, name: str, raw: object,
) -> None:
    task = f"synthetic__missing-applicability-{name}"
    deep = _complete_deep_metrics(task)
    target = (
        deep["behavioral_impact"]
        if section == "behavioral_impact"
        else deep["performance"][section]
    )
    target[name] = raw
    deep["metric_applicability"].get(section, {}).pop(name, None)
    _write_task(tmp_path, task, deep_metrics=deep)

    records, missing, _source = metrics._performance_feature_records(
        task, str(tmp_path)
    )

    assert records[name]["status"] == "UNMEASURED"
    assert f"PERF.{name}" in missing


@pytest.mark.parametrize(
    ("section", "name", "raw"),
    [
        ("localization", "gold_rank", None),
        ("edit_quality", "first_edit_correctness", {}),
        ("behavioral_impact", "per_tag_impact", {}),
    ],
)
def test_task_perf_explicit_non_applicability_is_preserved(
    tmp_path: Path, section: str, name: str, raw: object,
) -> None:
    task = f"synthetic__explicit-applicability-{name}"
    deep = _complete_deep_metrics(task)
    target = (
        deep["behavioral_impact"]
        if section == "behavioral_impact"
        else deep["performance"][section]
    )
    target[name] = raw
    deep["metric_applicability"].setdefault(section, {})[name] = {
        "applicable": False,
        "predicate": "fixture opportunity exists",
        "reason": "fixture has no applicable opportunity",
    }
    _write_task(tmp_path, task, deep_metrics=deep)

    records, missing, _source = metrics._performance_feature_records(
        task, str(tmp_path)
    )

    assert records[name]["status"] == "NOT_APPLICABLE"
    assert f"PERF.{name}" not in missing
    assert records[name]["applicability"] == {
        "applicable": False,
        "predicate": "fixture opportunity exists",
        "reason": "fixture has no applicable opportunity",
    }


def test_task_perf_right_censoring_is_preserved_without_live_promotion(
    tmp_path: Path,
) -> None:
    task = "synthetic__right-censored"
    deep = _complete_deep_metrics(task)
    deep["performance"]["localization"]["files_to_gold_view"] = None
    deep["metric_applicability"].setdefault("localization", {})[
        "files_to_gold_view"
    ] = {
        "applicable": True,
        "predicate": "n_gold_files > 0",
        "reason": "true gold file denominator is available",
        "observation": {
            "state": "RIGHT_CENSORED",
            "event": "first_gold_view",
            "clock": "unique_files",
            "lower_bound": 3,
            "terminal_horizon": 3,
            "terminal_status": "Submitted",
        },
    }
    deep["performance"].setdefault("metric_applicability", {}).setdefault(
        "localization", {}
    )["files_to_gold_view"] = copy.deepcopy(
        deep["metric_applicability"]["localization"]["files_to_gold_view"]
    )
    _write_task(tmp_path, task, deep_metrics=deep)

    records, missing, _source = metrics._performance_feature_records(
        task, str(tmp_path)
    )
    row = records["files_to_gold_view"]
    readiness = metrics._measurement_only_readiness(row)

    assert row["status"] == "RIGHT_CENSORED"
    assert row["value"] is None
    assert row["observation"]["lower_bound"] == 3
    assert "PERF.files_to_gold_view" not in missing
    assert readiness["gates"]["applicability_resolved"] is True
    assert readiness["live_witness"] is False
    assert readiness["ss_live"] is False


def test_run_perf_right_censoring_requires_exact_task_identity(
    tmp_path: Path,
) -> None:
    """A matching censor count cannot hide a run/task population misjoin."""
    task = "synthetic__right-censored"
    deep = _complete_deep_metrics(task)
    deep["performance"]["localization"]["files_to_gold_view"] = None
    deep["metric_applicability"].setdefault("localization", {})[
        "files_to_gold_view"
    ] = {
        "applicable": True,
        "predicate": "n_gold_files > 0",
        "reason": "true gold file denominator is available",
        "observation": {
            "state": "RIGHT_CENSORED",
            "event": "first_gold_view",
            "clock": "unique_files",
            "lower_bound": 3,
            "terminal_horizon": 3,
            "terminal_status": "Submitted",
        },
    }
    deep["performance"].setdefault("metric_applicability", {}).setdefault(
        "localization", {}
    )["files_to_gold_view"] = copy.deepcopy(
        deep["metric_applicability"]["localization"]["files_to_gold_view"]
    )
    _write_task(tmp_path, task, deep_metrics=deep)
    task_records, _missing, _source = metrics._performance_feature_records(
        task, str(tmp_path)
    )
    run_metrics = gt_run_metrics.aggregate_run_metrics(
        [deep], expected_task_ids=[task]
    )
    run_metrics["run_id"] = "run-1"
    run_metric = run_metrics["mandatory_performance"]["localization"][
        "files_to_gold_view"
    ]
    run_path = tmp_path / "gt_run_metrics.json"
    run_path.write_text(json.dumps(run_metrics), encoding="utf-8")

    task_rows = [{**task_records["files_to_gold_view"], "_task": task}]
    valid = metrics._run_distribution_feature_record(
        "run-1",
        str(run_path),
        task_rows,
        section="localization",
        name="files_to_gold_view",
        value_type="nonnegative_int",
        expected_tasks=1,
    )
    assert valid["status"] == "MEASURED"
    assert "_task" not in valid
    assert valid["run_aggregate"] == run_metric

    run_metric["right_censored_tasks"] = ["synthetic__different-task"]
    run_path.write_text(json.dumps(run_metrics), encoding="utf-8")

    row = metrics._run_distribution_feature_record(
        "run-1",
        str(run_path),
        task_rows,
        section="localization",
        name="files_to_gold_view",
        value_type="nonnegative_int",
        expected_tasks=1,
    )

    assert row["status"] == "UNMEASURED"
    assert row["metric_structure_valid"] is False


def test_run_perf_not_applicable_requires_exact_task_identity(tmp_path: Path) -> None:
    """A matching N/A count cannot substitute a different task identity."""
    task = "synthetic__not-applicable"
    deep = _complete_deep_metrics(task)
    deep["performance"]["scope_completeness"]["multi_file_discovery"] = None
    applicability = {
        "applicable": False,
        "predicate": "n_gold_files > 1",
        "reason": "fixture has no multi-file gold scope",
    }
    deep["metric_applicability"].setdefault("scope_completeness", {})[
        "multi_file_discovery"
    ] = applicability
    deep["performance"].setdefault("metric_applicability", {}).setdefault(
        "scope_completeness", {}
    )["multi_file_discovery"] = copy.deepcopy(applicability)
    _write_task(tmp_path, task, deep_metrics=deep)
    task_records, _missing, _source = metrics._performance_feature_records(
        task, str(tmp_path)
    )
    run_metrics = gt_run_metrics.aggregate_run_metrics(
        [deep], expected_task_ids=[task]
    )
    run_metrics["run_id"] = "run-1"
    run_metric = run_metrics["mandatory_performance"]["scope_completeness"][
        "multi_file_discovery"
    ]
    run_path = tmp_path / "gt_run_metrics.json"
    run_path.write_text(json.dumps(run_metrics), encoding="utf-8")
    task_rows = [{**task_records["multi_file_discovery"], "_task": task}]

    valid = metrics._run_distribution_feature_record(
        "run-1", str(run_path), task_rows,
        section="scope_completeness", name="multi_file_discovery",
        value_type="bool", expected_tasks=1,
    )
    assert valid["status"] == "NOT_APPLICABLE"
    assert valid["run_aggregate"] == run_metric

    run_metric["not_applicable_tasks"] = ["synthetic__different-task"]
    run_path.write_text(json.dumps(run_metrics), encoding="utf-8")
    invalid = metrics._run_distribution_feature_record(
        "run-1", str(run_path), task_rows,
        section="scope_completeness", name="multi_file_discovery",
        value_type="bool", expected_tasks=1,
    )
    assert invalid["status"] == "UNMEASURED"
    assert invalid["metric_structure_valid"] is False


def test_deep_metrics_lookup_is_task_exact_and_matches_live_layout(tmp_path: Path) -> None:
    task = "synthetic__layout"
    task_dir = tmp_path / "gt" / task
    _write_task(task_dir, task)
    (tmp_path / f"gt_deep_metrics_{task}.json").write_text(
        json.dumps(_complete_deep_metrics(task)), encoding="utf-8"
    )
    (task_dir / "gt_deep_metrics_someone_else.json").write_text(
        json.dumps(_complete_deep_metrics("someone_else")), encoding="utf-8"
    )

    records, missing, source = metrics._performance_feature_records(task, str(task_dir))

    assert source == str(tmp_path / f"gt_deep_metrics_{task}.json")
    assert missing == []
    assert records["gold_rank"]["status"] == "MEASURED"


def test_generic_brief_sidecar_cannot_cross_task_boundary(tmp_path: Path) -> None:
    task_dir = tmp_path / "run" / "task-a"
    _write_task(task_dir, "task-a")
    (tmp_path / "brief_result.json").write_text("{}", encoding="utf-8")

    assert metrics._find_named_input(
        str(task_dir), "brief_result.json", locations=2
    ) is None


def test_malformed_present_brief_is_collection_failure(tmp_path: Path) -> None:
    task = "synthetic__bad-brief"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))
    (tmp_path / "brief_result.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="malformed required ACQ artifact"):
        metrics.collect_task(task, str(tmp_path), profile="2")


def test_aggregate_requires_every_canonical_row_from_every_task(tmp_path: Path) -> None:
    task = "synthetic__aggregate"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))
    record = metrics.collect_task(task, str(tmp_path), profile="2")
    del record["ss_features"]["gold_rank"]

    aggregate = metrics.aggregate_run("run-x", [record], profile="2")

    assert aggregate["ss_integrity"]["publishable"] is False
    assert aggregate["ss_integrity"]["missing_records"] == [
        {"task": task, "family": "PERF", "feature": "gold_rank"}
    ]

    record = metrics.collect_task(task, str(tmp_path), profile="2")
    record["ss_features"]["unexpected"] = {"family": "PERF", "status": "MEASURED"}
    record["ss_integrity"]["inventory_complete"] = False
    aggregate = metrics.aggregate_run("run-extra", [record], profile="2")
    assert aggregate["ss_integrity"]["publishable"] is False
    assert aggregate["ss_integrity"]["tasks_with_inventory_drift"] == [task]


def test_aggregate_preserves_expected_population_when_task_record_is_missing(
    tmp_path: Path,
) -> None:
    task = "synthetic__observed"
    missing = "synthetic__pre-agent-failure"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))
    record = metrics.collect_task(task, str(tmp_path), profile="2")

    aggregate = metrics.aggregate_run(
        "run-population", [record], profile="2",
        expected_task_ids=[task, missing],
    )

    assert aggregate["run_metrics"]["n_tasks"] == 2
    assert aggregate["run_metrics"]["observed_task_count"] == 1
    assert aggregate["integrity"]["missing_task_records"] == [missing]
    assert aggregate["ss_integrity"]["missing_task_records"] == [missing]
    assert aggregate["integrity"]["publishable"] is False
    assert aggregate["ss_integrity"]["publishable"] is False
    assert aggregate["run_metrics"]["ss_features"]["gold_rank"][
        "present_in_tasks"
    ] == 1


def test_run_aggregate_keeps_task_coverage_but_requires_canonical_run_coverage(tmp_path: Path) -> None:
    task = "synthetic__perf-aggregate"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))
    record = metrics.collect_task(task, str(tmp_path), profile="2")

    aggregate = metrics.aggregate_run("run-perf", [record], profile="2")

    gold = aggregate["run_metrics"]["ss_features"]["gold_rank"]["ss_readiness"]
    assert gold["role"] == "measurement"
    assert gold["gates"]["task_coverage"] is True
    assert gold["gates"]["aggregate_coverage"] is False
    assert gold["measurement_complete"] is False
    assert gold["ss_live"] is False
    cost = aggregate["run_metrics"]["ss_features"]["cost_per_resolved"]["ss_readiness"]
    assert cost["gates"]["artifact_valid"] is False
    assert cost["measurement_complete"] is False


def test_run_aggregate_joins_validated_run_ratio_artifact(tmp_path: Path) -> None:
    task = "synthetic__run-ratio"
    deep = _complete_deep_metrics(task)
    deep["resolved"] = True
    deep["performance"]["token_efficiency"]["total_cost_usd"] = 2.5
    _write_task(tmp_path, task, deep_metrics=deep)
    record = metrics.collect_task(task, str(tmp_path), profile="2")
    run_payload = gt_run_metrics.aggregate_run_metrics([deep], expected_task_ids=[task])
    run_payload["run_id"] = "run-ratio"
    run_path = tmp_path / "gt_run_metrics_run-ratio.json"
    run_path.write_text(json.dumps(run_payload), encoding="utf-8")

    aggregate = metrics.aggregate_run(
        "run-ratio", [record], profile="2", run_metrics_artifact=str(run_path)
    )

    cost = aggregate["run_metrics"]["ss_features"]["cost_per_resolved"]
    assert cost["measurement"]["value"] == 2.5
    assert cost["measurement"]["run_aggregate"] == run_payload[
        "mandatory_performance"
    ]["token_efficiency"]["cost_per_resolved"]
    assert cost["measurement"]["source_artifact"] == run_path.name
    assert all(cost["ss_readiness"]["gates"].values())
    assert cost["ss_readiness"]["measurement_complete"] is True


@pytest.mark.parametrize("applicability", [None, {"applicable": False}])
def test_run_ratio_requires_matching_explicit_applicability(
    tmp_path: Path, applicability: dict | None,
) -> None:
    task = "synthetic__run-ratio-applicability"
    deep = _complete_deep_metrics(task)
    deep["resolved"] = True
    deep["performance"]["token_efficiency"]["total_cost_usd"] = 2.5
    _write_task(tmp_path, task, deep_metrics=deep)
    record = metrics.collect_task(task, str(tmp_path), profile="2")
    run_payload = gt_run_metrics.aggregate_run_metrics([deep], expected_task_ids=[task])
    run_payload["run_id"] = "run-ratio-applicability"
    run_payload["mandatory_performance"]["token_efficiency"][
        "cost_per_resolved"
    ]["applicability"] = applicability
    run_path = tmp_path / "gt_run_metrics_run-ratio-applicability.json"
    run_path.write_text(json.dumps(run_payload), encoding="utf-8")

    aggregate = metrics.aggregate_run(
        "run-ratio-applicability", [record], profile="2",
        run_metrics_artifact=str(run_path),
    )

    cost = aggregate["run_metrics"]["ss_features"]["cost_per_resolved"]
    assert cost["measurement"]["status"] == "UNMEASURED"
    assert cost["ss_readiness"]["gates"]["applicability_resolved"] is False


def test_run_ratio_accepts_explicit_non_applicability_for_zero_resolves(
    tmp_path: Path,
) -> None:
    task = "synthetic__run-ratio-not-applicable"
    deep = _complete_deep_metrics(task)
    deep["resolved"] = False
    deep["performance"]["token_efficiency"]["total_cost_usd"] = 2.5
    _write_task(tmp_path, task, deep_metrics=deep)
    record = metrics.collect_task(task, str(tmp_path), profile="2")
    run_payload = gt_run_metrics.aggregate_run_metrics([deep], expected_task_ids=[task])
    run_payload["run_id"] = "run-ratio-not-applicable"
    run_path = tmp_path / "gt_run_metrics_run-ratio-not-applicable.json"
    run_path.write_text(json.dumps(run_payload), encoding="utf-8")

    aggregate = metrics.aggregate_run(
        "run-ratio-not-applicable", [record], profile="2",
        run_metrics_artifact=str(run_path),
    )

    cost = aggregate["run_metrics"]["ss_features"]["cost_per_resolved"]
    assert cost["measurement"]["status"] == "NOT_APPLICABLE"
    assert cost["measurement"]["applicability"]["applicable"] is False
    assert all(cost["ss_readiness"]["gates"].values())


@pytest.mark.parametrize("artifact", ["missing", "malformed"])
def test_run_ratio_artifact_missing_or_malformed_fails_closed(
    tmp_path: Path, artifact: str,
) -> None:
    task = "synthetic__bad-run-ratio"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))
    record = metrics.collect_task(task, str(tmp_path), profile="2")
    path = tmp_path / "gt_run_metrics_bad.json"
    if artifact == "malformed":
        path.write_text(json.dumps({"schema": "wrong", "run_id": "run-bad"}), encoding="utf-8")

    aggregate = metrics.aggregate_run(
        "run-bad", [record], profile="2", run_metrics_artifact=str(path)
    )

    cost = aggregate["run_metrics"]["ss_features"]["cost_per_resolved"]
    assert cost["ss_readiness"]["gates"]["artifact_valid"] is False
    assert cost["ss_readiness"]["measurement_complete"] is False


def _caller_contract_attestation(
    candidate_id: str, seal: str
) -> tuple[ProducerAttestation, dict[str, bytes]]:
    """A valid gateway ``caller_break`` attestation → the ``caller_contract`` class."""
    source = b'{"callers":3,"files":2}'
    ref = ArtifactRef(
        kind="producer_input",
        artifact_id="callers.json",
        sha256=hashlib.sha256(source).hexdigest(),
        revision="edit:11",
    )
    proof = ProofRef("producer_observation", ref, "$.callers")
    attestation = ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type="caller_break",
        runtime_producer_id="caller_contract",
        registered_producer_id="contract_map",
        candidate_id=candidate_id,
        delivery_seal=seal,
        source_artifacts=(ref,),
        truth_predicates=(
            PredicateAttestation(
                "TRUTH", "truth.callers", "callers", "preserved", "preserved", PASS,
                (proof,),
            ),
        ),
        freshness_predicates=(
            PredicateAttestation(
                FRESHNESS, "fresh.callers", "graph", "current", "current", PASS,
                (proof,),
            ),
        ),
        decision=DecisionBinding("how to modify a fn", "edit_result", "edit_result"),
    )
    return attestation, {"callers.json": source}


def test_attestation_join_populates_truth_only_for_attested_class(tmp_path: Path) -> None:
    task = "synthetic__attestation-join"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))
    candidate_id = "caller:src/pkg.py:11"
    seal = "d" * 16
    attestation, artifacts = _caller_contract_attestation(candidate_id, seal)
    persist_attestation(
        attestation, artifacts, tmp_path / "art" / "producer_attestations"
    )
    payload = "src/pkg.py:11 preserve callers"
    assert hashlib.sha256(payload.encode()).hexdigest()[:16] != seal  # seal is the join key
    (tmp_path / f"gt_runtime_ledger_{task}.jsonl").write_text(json.dumps({
        "layer": "l3.contract",
        "event_type": "edit_result",
        "outcome": "delivered",
        "chars_delivered": len(payload),
        "content_sha256_16": seal,
        "candidate_id": candidate_id,
        "evidence_type": "caller_break",
        "fact_class": "caller_contract",
        "file_path": "src/pkg.py",
    }) + "\n", encoding="utf-8")
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(json.dumps({
        "messages": [{"role": "tool", "content": payload}],
        "info": {"submission": ""},
    }), encoding="utf-8")

    record = metrics.collect_task(task, str(tmp_path), profile="2")

    # The attested class receives joined truth from the validated, seal-joined attestation.
    truth = record["fact_classes"]["caller_contract"]["truth_valid"]
    assert truth["status"] == "MEASURED"
    assert truth["value"] is True
    assert truth["source_artifact"] == "producer_attestations"
    # J2 scopes to TRUTH only. authority_valid is a separate leg the attestation does
    # not carry, so it stays UNMEASURED and the composite correct_info gate (truth AND
    # authority) is honestly None — not fabricated True.
    assert record["fact_classes"]["caller_contract"]["authority_valid"]["status"] == "UNMEASURED"
    fact_readiness = record["ss_features"]["caller_contract"]["ss_readiness"]
    assert fact_readiness["gates"]["correct_info"] is None

    # Every other class stays UNMEASURED — no attestation, no inference.
    for other in ("localization", "def_partition", "obligations", "syntax_result"):
        assert record["fact_classes"][other]["truth_valid"]["status"] == "UNMEASURED"

    diag = record["ss_integrity"]["attestation_join"]
    assert diag["attestations_loaded"] == 1
    assert diag["applied_truth_overrides"] == ["caller_contract"]
    assert diag["joined_fact_classes"]["caller_contract"]["truth"] is True


def test_attestation_join_seal_mismatch_leaves_truth_unmeasured(tmp_path: Path) -> None:
    task = "synthetic__attestation-nojoin"
    _write_task(tmp_path, task, deep_metrics=_complete_deep_metrics(task))
    candidate_id = "caller:src/pkg.py:11"
    attestation, artifacts = _caller_contract_attestation(candidate_id, "d" * 16)
    persist_attestation(
        attestation, artifacts, tmp_path / "art" / "producer_attestations"
    )
    payload = "src/pkg.py:11 preserve callers"
    (tmp_path / f"gt_runtime_ledger_{task}.jsonl").write_text(json.dumps({
        "layer": "l3.contract", "event_type": "edit_result", "outcome": "delivered",
        "chars_delivered": len(payload),
        "content_sha256_16": "e" * 16,  # seal does NOT match the attestation
        "candidate_id": candidate_id,
        "evidence_type": "caller_break", "fact_class": "caller_contract",
        "file_path": "src/pkg.py",
    }) + "\n", encoding="utf-8")
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(json.dumps({
        "messages": [{"role": "tool", "content": payload}],
        "info": {"submission": ""},
    }), encoding="utf-8")

    record = metrics.collect_task(task, str(tmp_path), profile="2")

    assert record["fact_classes"]["caller_contract"]["truth_valid"]["status"] == "UNMEASURED"
    assert record["ss_features"]["caller_contract"]["ss_readiness"]["gates"]["correct_info"] is None
    diag = record["ss_integrity"]["attestation_join"]
    assert diag["attestations_loaded"] == 1
    assert diag["applied_truth_overrides"] == []


def test_live_collect_gate_validates_canonical_128_integrity_not_just_file_presence() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("_MM_MISSING")
    end = workflow.index("GT_METRICS_COMPLETE", start)
    gate = workflow[start:end]

    assert "ss_integrity" in gate
    assert "inventory_complete" in gate
    assert "required_inputs_complete" in gate
    assert "ss_features" in gate and "128" in gate
