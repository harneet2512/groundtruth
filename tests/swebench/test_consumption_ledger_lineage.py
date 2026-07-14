from __future__ import annotations

import hashlib
import json

from scripts.swebench.consumption_ledger import build_consumption_ledger


def _row(payload: str) -> dict:
    return {
        "layer": "gateway.signature_mismatch",
        "event_type": "edit_result",
        "iteration": 1,
        "outcome": "delivered",
        "chars_delivered": len(payload),
        "content_sha256_16": hashlib.sha256(payload.encode()).hexdigest()[:16],
        "file_path": "src/pkg.py",
        "lineage_schema": "gt.feature_lineage.v1",
        "runtime_producer_id": "patch_delta",
        "registered_producer_id": "patch_delta",
        "producer_registration_match": True,
        "evidence_type": "signature_mismatch",
        "fact_class": "signature_delta",
        "feature_ids": [
            {"category": "CAP", "feature_id": "GT_PATCH_DELTA", "role": "byte_owner"},
            {"category": "FACT", "feature_id": "signature_delta", "role": "fact"},
        ],
        "required_event": "edit_result",
        "actual_event": "edit_result",
        "receipt_predicate": "updated_callers_for_delta",
        "causal_eval": "paired_caller_breakage_rate",
        "causal_probe_id": "",
        "causal_contribution_proven": False,
        "reactive": False,
    }


def test_exact_seal_join_carries_typed_lineage_but_not_causal_credit(tmp_path) -> None:
    payload = "src/pkg.py:12: preserve parse_config signature"
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(_row(payload)) + "\n", encoding="utf-8")
    trajectory = {"messages": [
        {"role": "tool", "content": payload},
        {"role": "assistant", "content": "The pkg.py signature applies.",
         "tool_calls": [{"function": {"arguments": json.dumps(
             {"command": "sed -i 's/old/new/' src/pkg.py"}
         )}}]},
    ]}

    entry = build_consumption_ledger(
        trajectory, runtime_ledger_path=str(ledger)
    )["entries"][0]

    assert entry["join_method"] == "seal"
    assert entry["receipt"] == 3
    assert entry["feature_lineage"]["fact_class"] == "signature_delta"
    assert entry["feature_lineage"]["producer_registration_match"] is True
    assert entry["feature_lineage"]["causal_contribution_proven"] is False


def test_legacy_join_cannot_promote_typed_feature_lineage(tmp_path) -> None:
    payload = '<gt-evidence file="src/pkg.py">signature</gt-evidence>'
    row = _row(payload)
    row.pop("content_sha256_16")
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
    trajectory = {"messages": [{"role": "tool", "content": payload}]}

    entry = build_consumption_ledger(
        trajectory, runtime_ledger_path=str(ledger)
    )["entries"][0]

    assert entry["join_method"] == "legacy_content"
    assert entry.get("feature_lineage") is None


def test_invalid_cap_role_cannot_enter_exact_seal_join(tmp_path) -> None:
    payload = "src/pkg.py:12: preserve parse_config signature"
    row = _row(payload)
    row["feature_ids"][0]["role"] = "mediator"
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")

    entry = build_consumption_ledger(
        {"messages": [{"role": "tool", "content": payload}]},
        runtime_ledger_path=str(ledger),
    )["entries"][0]

    assert entry["join_method"] == "seal"
    assert entry.get("feature_lineage") is None


def test_self_attested_wrong_producer_cannot_enter_exact_seal_join(tmp_path) -> None:
    payload = "src/pkg.py:12: preserve parse_config signature"
    row = _row(payload)
    row["runtime_producer_id"] = "change_surface"
    row["producer_registration_match"] = True
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")

    entry = build_consumption_ledger(
        {"messages": [{"role": "tool", "content": payload}]},
        runtime_ledger_path=str(ledger),
    )["entries"][0]

    assert entry["join_method"] == "seal"
    assert entry.get("feature_lineage") is None


def test_tampered_registry_fields_cannot_enter_exact_seal_join(tmp_path) -> None:
    payload = "src/pkg.py:12: preserve parse_config signature"
    row = _row(payload)
    row["required_event"] = "submit"
    row["causal_contribution_proven"] = True
    row["causal_probe_id"] = "self-attested"
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")

    entry = build_consumption_ledger(
        {"messages": [{"role": "tool", "content": payload}]},
        runtime_ledger_path=str(ledger),
    )["entries"][0]

    assert entry["join_method"] == "seal"
    assert entry.get("feature_lineage") is None
