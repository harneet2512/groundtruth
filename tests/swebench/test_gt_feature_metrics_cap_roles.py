from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_feature_metrics as metrics  # noqa: E402  # pyright: ignore[reportMissingImports]
from groundtruth.runtime import fact_registry  # noqa: E402
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    CAP_BYTE_OWNER_MECHANISMS,
    CAP_BYTE_OWNER_IDS,
    CAP_ELIGIBILITY_IDS,
    CAP_MEDIATOR_IDS,
)


def _fact_lifecycles() -> dict[str, dict]:
    fact = metrics.new_lifecycle("fixture")
    fact["eligible"] = metrics.measured(True, source_artifact="ledger")
    fact["produced"] = metrics.measured(True, source_artifact="ledger")
    fact["delivered"] = metrics.measured(True, source_artifact="ledger")
    fact["receipt_level"] = metrics.measured(3, source_artifact="trajectory")
    return {
        name: copy.deepcopy(fact) for name in fact_registry.all_fact_classes()
    }


def _infra() -> dict:
    return {
        "l6_staged": False, "l6_reindex": 0, "arbiter_candidates": 0,
        "arbiter_lost": 0, "dose_suppressed": 0,
        "any_produced": True, "any_delivered": True,
    }


def test_cap_role_authority_partitions_all_profile_members() -> None:
    assert len(CAP_BYTE_OWNER_IDS) == 8
    assert len(CAP_ELIGIBILITY_IDS) == 13
    assert len(CAP_MEDIATOR_IDS) == 26
    assert set(metrics._DIRECT_MEMBER_FACTCLASS) == set(CAP_BYTE_OWNER_IDS)
    assert metrics._DIRECT_MEMBER_FACTCLASS["GT_SS_COHERENCE_V2"] is None
    assert metrics.layer_to_fact_class("detect.coherence") is None
    assert set(metrics._DIRECT_MEMBER_FACTCLASS) == set(CAP_BYTE_OWNER_MECHANISMS)
    assert set(metrics._INFRA_MEMBER_MEDIATES) == (
        set(CAP_ELIGIBILITY_IDS) | set(CAP_MEDIATOR_IDS)
    )


def test_import_crosscheck_rejects_role_table_drift(monkeypatch) -> None:
    monkeypatch.setitem(
        metrics._DIRECT_MEMBER_FACTCLASS, "GT_VERIFY_EXECUTE", "covering_red"
    )
    monkeypatch.delitem(metrics._INFRA_MEMBER_MEDIATES, "GT_VERIFY_EXECUTE")
    with pytest.raises(ValueError, match="byte-owner table drift"):
        metrics._import_time_crosscheck()


def test_import_crosscheck_rejects_fabricated_coherence_fact(monkeypatch) -> None:
    monkeypatch.setitem(
        metrics._DIRECT_MEMBER_FACTCLASS, "GT_SS_COHERENCE_V2", "recovery"
    )
    with pytest.raises(ValueError, match="FACT projection drift"):
        metrics._import_time_crosscheck()


def test_mediator_and_eligibility_records_never_copy_fact_delivery() -> None:
    for member, expected_role in (
        ("GT_VERIFY_EXECUTE", "mediator"),
        ("GT_SS_ELIGIBILITY", "eligibility"),
    ):
        record = metrics.member_record(
            member, _fact_lifecycles(), _infra(), metrics.BASELINE_UNAVAILABLE,
            ledger_artifact="ledger", traj_artifact="trajectory",
        )
        assert record["cap_role"] == expected_role
        assert record["lifecycle"]["delivered"]["status"] == "NOT_ELIGIBLE"
        assert record["lifecycle"]["receipt_level"]["status"] == "NOT_ELIGIBLE"


def _seal_entry(layer: str, seal: str = "a" * 16, chars: int = 12) -> dict:
    return {
        "source": "trajectory", "joined": True, "join_method": "seal",
        "content_sha256_16": seal, "chars": chars, "ledger_layer": layer,
    }


def test_typed_lineage_owner_requires_exact_cap_ref_binding_and_seal() -> None:
    row = {
        "layer": "gateway.signature_mismatch",
        "outcome": "delivered",
        "chars_delivered": 12,
        "content_sha256_16": "a" * 16,
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
    }
    assert metrics._member_delivery_byte_proven(
        "GT_PATCH_DELTA", [row],
        {"entries": [_seal_entry("gateway.signature_mismatch")]},
    ) is True
    wrong_binding = {**row, "runtime_producer_id": "change_surface"}
    assert metrics._member_delivery_byte_proven(
        "GT_PATCH_DELTA", [wrong_binding],
        {"entries": [_seal_entry("gateway.signature_mismatch")]},
    ) is False
    missing_cap_ref = {
        **row,
        "feature_ids": [
            {"category": "FACT", "feature_id": "signature_delta", "role": "fact"}
        ],
    }
    assert metrics._member_delivery_byte_proven(
        "GT_PATCH_DELTA", [missing_cap_ref],
        {"entries": [_seal_entry("gateway.signature_mismatch")]},
    ) is False


def test_exact_profile_owner_requires_authorized_layer_and_seal() -> None:
    row = {
        "profile_member": "GT_EDIT_CHECK", "layer": "edit.syntax",
        "outcome": "delivered", "chars_delivered": 12,
        "content_sha256_16": "a" * 16,
    }
    assert metrics._member_delivery_byte_proven(
        "GT_EDIT_CHECK", [row], {"entries": [_seal_entry("edit.syntax")]},
    ) is True
    assert metrics._member_delivery_byte_proven(
        "GT_EDIT_CHECK", [{**row, "layer": "recovery"}],
        {"entries": [_seal_entry("recovery")]},
    ) is False

    forged = {**row, "profile_member": "GT_VERIFY_EXECUTE"}
    assert metrics._member_delivery_byte_proven(
        "GT_VERIFY_EXECUTE", [forged], {"entries": [_seal_entry("edit.syntax")]}
    ) is False


def test_coherence_exact_delivery_has_own_lifecycle_but_cannot_be_ss_live() -> None:
    row = {
        "profile_member": "GT_SS_COHERENCE_V2", "layer": "detect.coherence",
        "outcome": "delivered", "chars_delivered": 12,
        "content_sha256_16": "a" * 16,
    }
    entry = {**_seal_entry("detect.coherence"), "receipt": 2, "msg_index": 7}
    record = metrics.member_record(
        "GT_SS_COHERENCE_V2", _fact_lifecycles(), _infra(),
        metrics.BASELINE_UNAVAILABLE, ledger_artifact="ledger",
        traj_artifact="trajectory", owner_rows=[row],
        consumption_ledger={"entries": [entry]},
    )
    assert record["fact_classes"] == []
    assert record["lifecycle"]["delivered"]["value"] is True
    assert record["lifecycle"]["receipt_level"]["value"] == 2
    assert record["lifecycle"]["truth_valid"]["status"] == "UNMEASURED"
    readiness = metrics.ss_gate_readiness(
        record["lifecycle"], byte_proven=metrics._member_delivery_byte_proven(
            "GT_SS_COHERENCE_V2", [row], {"entries": [entry]}
        ), leak_free=True, dose_ok=True, fair_probe=None, live_witness=False,
    )
    assert readiness["gates"]["delivered_byte_proven"] is True
    assert readiness["gates"]["correct_info"] is None
    assert readiness["gates"]["correct_rl_adhered_time"] is None
    assert readiness["ss_live"] is False
