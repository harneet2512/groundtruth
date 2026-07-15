from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for import_path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from scripts.swebench.gt_feature_inventory import canonical_feature_inventory  # noqa: E402
from scripts.swebench.ss_live_coverage_campaign import (  # noqa: E402
    load_selection_signals,
    load_structural_opportunities,
    main as campaign_main,
    merge_selection_signals,
    select_campaign,
)


def _inventory():
    return {"ACQ": ("a",), "CAP": ("c",), "FACT": ("f",), "PERF": ("p",)}


def _source(name: str = "run/task.json") -> dict[str, str]:
    return {"path": name, "sha256": "a" * 64}


def _document(task: str, rows: dict, **extra) -> dict:
    return {
        "schema": "gt.feature_metrics.v1",
        "profile": "2",
        "task": task,
        "ss_features": rows,
        "_selection_source": _source(),
        **extra,
    }


def test_campaign_keeps_locked_task_and_adds_only_tasks_with_new_opportunities():
    documents = [
        _document("t1", {
            "a": {"family": "ACQ", "ss_readiness": {
                "gates": {"candidate_local_contribution": True}}},
            "f": {"family": "FACT", "ss_readiness": {
                "gates": {"delivered_byte_proven": True}}},
        }),
        _document("t2", {
            "c": {"family": "CAP", "ss_readiness": {
                "cap_role": "byte_owner", "gates": {
                    "delivered_byte_proven": True}}},
            "p": {"family": "PERF", "status": "MEASURED"},
        }),
    ]
    signals, evidence = load_selection_signals(
        documents, _inventory(), cap_roles={"c": "byte_owner"},
        fact_roles={"f": "fact_delivery"},
    )

    result = select_campaign(_inventory(), signals, evidence, locked_tasks=["t1"])

    assert result["campaign_tasks"] == ["t1", "t2"]
    assert result["planned_opportunity_rows"] == 4
    assert result["unplanned_opportunity_rows"] == []
    assert all(row["terminal_live_proof_required"] for row in result["rows"])


def test_invalid_schema_profile_and_family_are_rejected_as_selection_inputs():
    valid = _document("valid", {"f": {
        "family": "FACT", "ss_readiness": {
            "gates": {"delivered_byte_proven": True}},
    }})
    bad_schema = {**valid, "task": "bad-schema", "schema": "legacy"}
    bad_profile = {**valid, "task": "bad-profile", "profile": "1"}
    wrong_family = _document("wrong-family", {"f": {
        "family": "CAP", "ss_readiness": {
            "gates": {"delivered_byte_proven": True}},
    }})

    signals, _ = load_selection_signals(
        [bad_schema, bad_profile, wrong_family, valid], _inventory(),
        cap_roles={"c": "byte_owner"}, fact_roles={"f": "fact_delivery"},
    )

    assert signals == {"f": {"valid"}}


def test_internal_fact_never_borrows_legacy_produced_or_delivery_signal():
    document = _document(
        "legacy",
        {"f": {"family": "FACT", "ss_readiness": {
            "gates": {"delivered_byte_proven": True}}}},
        fact_classes={"f": {"produced": {"value": True}}},
    )

    signals, _ = load_selection_signals(
        [document], _inventory(), cap_roles={"c": "byte_owner"},
        fact_roles={"f": "internal_support"},
    )

    assert "f" not in signals


def test_internal_fact_accepts_only_typed_runtime_support_opportunity():
    document = _document("support", {"f": {
        "family": "FACT",
        "ss_readiness": {
            "role": "internal_support",
            "gates": {"runtime_support_receipt": True},
        },
    }})

    signals, evidence = load_selection_signals(
        [document], _inventory(), cap_roles={"c": "byte_owner"},
        fact_roles={"f": "internal_support"},
    )

    assert signals == {"f": {"support"}}
    assert evidence[("f", "support")][0]["reason"] == "historical_internal_support_receipt"


def test_cap_roles_use_byte_owner_opportunity_or_typed_control_participation():
    inventory = {"ACQ": (), "CAP": ("owner", "control"), "FACT": (), "PERF": ()}
    document = _document("task", {
        "owner": {
            "family": "CAP",
            "opportunity_evidence": {"status": "BOUND"},
            "ss_readiness": {},
        },
        "control": {
            "family": "CAP",
            "ss_readiness": {
                "role": "infra_control",
                "gates": {"runtime_member_control_receipt": True},
                "mediation": {"runtime_linked_fact_ids": ["f"]},
            },
        },
    })

    signals, evidence = load_selection_signals(
        [document], inventory,
        cap_roles={"owner": "byte_owner", "control": "mediator"},
        fact_roles={},
    )

    assert signals == {"owner": {"task"}, "control": {"task"}}
    assert evidence[("owner", "task")][0]["reason"] == (
        "historical_bound_byte_owner_opportunity"
    )
    assert evidence[("control", "task")][0]["reason"] == (
        "historical_typed_control_participation"
    )


def test_perf_is_a_campaign_wide_run_aggregation_obligation_including_run_ratio_na():
    result = select_campaign(_inventory(), {}, {}, locked_tasks=["paid-task"])

    perf = next(row for row in result["rows"] if row["feature_id"] == "p")
    assert perf["planned_opportunity_tasks"] == ["paid-task"]
    assert perf["selection_evidence"] == [{
        "reason": "planned_run_aggregation_obligation",
        "proof_scope": "selection_only_live_measurement_required",
    }]
    assert result["unplanned_opportunity_rows"] == ["a", "c", "f"]


def test_structural_manifest_is_source_hash_bound_and_maps_generic_kinds(
    tmp_path: Path,
):
    source = tmp_path / "frozen.jsonl"
    signature_patch = "--- a/src/module.py\n+++ b/src/module.py\n@@\n-def f():\n+def f(x):\n"
    new_file_patch = "--- /dev/null\n+++ b/src/new_module.py\n@@\n+VALUE = 1\n"
    source.write_text("\n".join((
        json.dumps({"instance_id": "signature-task", "model_patch": signature_patch}),
        json.dumps({"instance_id": "new-file-task", "model_patch": new_file_patch}),
        "",
    )), encoding="utf-8")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = tmp_path / "structural.json"
    manifest.write_text(json.dumps({
        "schema": "gt.ss_structural_opportunity_manifest.v1",
        "source_artifact": {"path": source.name, "sha256": source_sha},
        "opportunities": [{
            "task": "signature-task",
            "kind": "callable_signature_changed",
            "patch_sha256": hashlib.sha256(signature_patch.encode()).hexdigest(),
            "matched_paths": ["src/module.py"],
        }, {
            "task": "new-file-task",
            "kind": "source_file_added",
            "patch_sha256": hashlib.sha256(new_file_patch.encode()).hexdigest(),
            "matched_paths": ["src/new_module.py"],
            "retain": True,
        }],
    }), encoding="utf-8")

    signals, evidence = load_structural_opportunities(manifest)

    assert signals == {
        "signature_delta": {"signature-task"},
        "newfile_precedent": {"new-file-task"},
    }
    assert evidence[("signature_delta", "signature-task")][0]["source_sha256"] == source_sha
    campaign = select_campaign(
        _inventory(), signals, evidence, locked_tasks=["locked-task"]
    )
    assert campaign["campaign_tasks"][:2] == ["locked-task", "new-file-task"]

    detached = json.loads(manifest.read_text(encoding="utf-8"))
    detached["opportunities"][0]["patch_sha256"] = "d" * 64
    manifest.write_text(json.dumps(detached), encoding="utf-8")
    try:
        load_structural_opportunities(manifest)
    except ValueError as exc:
        assert "patch hash mismatch" in str(exc)
    else:
        raise AssertionError("a task opportunity must bind to its exact frozen patch")
    detached["opportunities"][0]["patch_sha256"] = hashlib.sha256(
        signature_patch.encode()
    ).hexdigest()
    manifest.write_text(json.dumps(detached), encoding="utf-8")

    source.write_text("changed\n", encoding="utf-8")
    try:
        load_structural_opportunities(manifest)
    except ValueError as exc:
        assert "source artifact hash mismatch" in str(exc)
    else:
        raise AssertionError("a detached structural manifest must fail closed")


def test_structural_manifest_rejects_forged_signature_kind_on_noncallable_diff(
    tmp_path: Path,
):
    patch = "--- a/src/module.py\n+++ b/src/module.py\n@@\n-VALUE = 1\n+VALUE = 2\n"
    source = tmp_path / "frozen.jsonl"
    source.write_text(json.dumps({
        "instance_id": "not-a-signature", "model_patch": patch,
    }) + "\n", encoding="utf-8")
    manifest = tmp_path / "structural.json"
    manifest.write_text(json.dumps({
        "schema": "gt.ss_structural_opportunity_manifest.v1",
        "source_artifact": {
            "path": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "opportunities": [{
            "task": "not-a-signature",
            "kind": "callable_signature_changed",
            "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            "matched_paths": ["src/module.py"],
        }],
    }), encoding="utf-8")

    try:
        load_structural_opportunities(manifest)
    except ValueError as exc:
        assert "no callable signature delta" in str(exc)
    else:
        raise AssertionError("a hand-authored kind cannot replace structural detection")


def test_structural_fact_opportunity_inherits_to_byte_owner_for_selection():
    base_signals = {"signature_delta": {"t"}}
    base_evidence = {("signature_delta", "t"): [{"reason": "structural"}]}
    signals, evidence = merge_selection_signals(
        base_signals,
        base_evidence,
        owner_facts={"GT_PATCH_DELTA": ("signature_delta",)},
    )

    assert signals["GT_PATCH_DELTA"] == {"t"}
    assert evidence[("GT_PATCH_DELTA", "t")][0]["reason"] == (
        "owned_fact_opportunity:signature_delta"
    )


def test_canonical_inventory_emits_exactly_128_selection_only_rows():
    inventory = canonical_feature_inventory()
    result = select_campaign(inventory, {}, {}, locked_tasks=["paid-task"])

    assert len(result["rows"]) == 128
    assert sum(len(inventory[family]) for family in inventory) == 128
    assert result["meaning"] == (
        "campaign planning only; no row is SS-LIVE proof or promotion"
    )
    assert not any("ss_live" in row for row in result["rows"])
    perf = next(row for row in result["rows"] if row["family"] == "PERF")
    assert perf["terminal_role"] == "measurement"
    assert perf["role_specific_obligation"] == "live_run_measurement"


def test_cli_creates_output_parent_and_preserves_source_provenance(
    tmp_path: Path, monkeypatch,
):
    metrics = tmp_path / "metrics"
    metrics.mkdir()
    payload = _document("task-1", {})
    payload.pop("_selection_source")
    source = metrics / "gt_feature_metrics_task-1.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    locked = tmp_path / "tasks.txt"
    locked.write_text("task-1\n", encoding="utf-8")
    output = tmp_path / "nested" / "campaign.json"

    monkeypatch.setattr(sys, "argv", [
        "ss_live_coverage_campaign.py",
        "--metrics-root", str(metrics),
        "--locked-tasks", str(locked),
        "--output", str(output),
    ])

    assert campaign_main() == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["campaign_tasks"] == ["task-1"]
