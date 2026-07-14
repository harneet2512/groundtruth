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
from groundtruth.runtime.feature_lineage import (
    CAP_BYTE_OWNER_IDS,
    CAP_ELIGIBILITY_IDS,
    CAP_MEDIATOR_IDS,
)  # noqa: E402


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


def test_only_byte_owner_can_consume_exact_member_delivery() -> None:
    row = {
        "profile_member": "GT_PATCH_DELTA",
        "layer": "gateway.signature_mismatch",
        "outcome": "delivered",
        "chars_delivered": 12,
        "content_sha256_16": "a" * 16,
    }
    entry = {
        "source": "trajectory", "joined": True, "join_method": "seal",
        "content_sha256_16": "a" * 16, "chars": 12,
        "ledger_layer": "gateway.signature_mismatch",
    }
    assert metrics._member_delivery_byte_proven(
        "GT_PATCH_DELTA", [row], {"entries": [entry]}
    ) is True

    forged = {**row, "profile_member": "GT_VERIFY_EXECUTE"}
    assert metrics._member_delivery_byte_proven(
        "GT_VERIFY_EXECUTE", [forged], {"entries": [entry]}
    ) is False
