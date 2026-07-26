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
    # P4 (B-TERM 2026-07-16): GT_SS_COHERENCE_V2 reclassified byte_owner → mediator. Counts move
    # WITHIN CAP (byte_owner 8→7, mediator 26→27).
    # ITEM 0 (2026-07-18): GT_POST_SEARCH added as an ELIGIBILITY member (eligibility 13→14,
    # CAP 47→48, inventory total 128→129).
    assert len(CAP_BYTE_OWNER_IDS) == 7
    assert len(CAP_ELIGIBILITY_IDS) == 14
    assert len(CAP_MEDIATOR_IDS) == 27
    assert metrics.cap_role_for("GT_POST_SEARCH") == "eligibility"
    assert "GT_SS_COHERENCE_V2" not in CAP_BYTE_OWNER_IDS
    assert metrics.cap_role_for("GT_SS_COHERENCE_V2") == "mediator"
    # It now MEDIATES the recovery FACT (coherence_collapse → recovery), never a fabricated own FACT.
    assert "GT_SS_COHERENCE_V2" not in metrics._DIRECT_MEMBER_FACTCLASS
    assert metrics._INFRA_MEMBER_MEDIATES["GT_SS_COHERENCE_V2"] == ("recovery",)
    assert metrics.layer_to_fact_class("detect.coherence") is None
    assert set(metrics._DIRECT_MEMBER_FACTCLASS) == set(CAP_BYTE_OWNER_IDS)
    assert set(metrics._DIRECT_MEMBER_FACTCLASS) == set(CAP_BYTE_OWNER_MECHANISMS)
    assert set(metrics._INFRA_MEMBER_MEDIATES) == (
        set(CAP_ELIGIBILITY_IDS) | set(CAP_MEDIATOR_IDS)
    )


def test_legacy_cochange_layer_maps_without_laundering_control_layers() -> None:
    assert metrics.layer_to_fact_class("l3.cochange") == "cochange_prior"
    assert metrics.layer_to_fact_class("detect.coherence") is None
    for layer in (
        "verify.horizon.advisory",
        "verify.horizon.urgent",
        "verify.horizon.gate",
        "verify.horizon.pivot",
        "verify.horizon.executed",
    ):
        assert metrics.layer_to_fact_class(layer) is None


def test_import_crosscheck_rejects_role_table_drift(monkeypatch) -> None:
    monkeypatch.setitem(
        metrics._DIRECT_MEMBER_FACTCLASS, "GT_VERIFY_EXECUTE", "covering_red"
    )
    monkeypatch.delitem(metrics._INFRA_MEMBER_MEDIATES, "GT_VERIFY_EXECUTE")
    with pytest.raises(ValueError, match="byte-owner table drift"):
        metrics._import_time_crosscheck()


def test_import_crosscheck_rejects_smuggling_coherence_back_as_byte_owner(monkeypatch) -> None:
    # P4: coherence must not be re-smuggled into the byte-owner FACT projection (a fabricated own
    # FACT for a control with no canonical FACT identity). The crosscheck rejects the added key.
    monkeypatch.setitem(
        metrics._DIRECT_MEMBER_FACTCLASS, "GT_SS_COHERENCE_V2", "recovery"
    )
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


def test_coherence_reclassified_as_mediator_grades_on_control_terminal() -> None:
    # P4 (B-TERM 2026-07-16): coherence is a CONTROL/mediator now — it mediates the recovery FACT
    # and is graded on the control terminal (live_control_mediation_effect), NOT the byte-owner bar
    # (which was unsatisfiable: its host-side measurement row is chars=0 → never a delivered row).
    record = metrics.member_record(
        "GT_SS_COHERENCE_V2", _fact_lifecycles(), _infra(),
        metrics.BASELINE_UNAVAILABLE, ledger_artifact="ledger",
        traj_artifact="trajectory",
    )
    assert record["cap_role"] == "mediator"
    assert record["fact_classes"] == ["recovery"]
    # A mediator never copies FACT delivery credit into its own lifecycle.
    assert record["lifecycle"]["delivered"]["status"] == "NOT_ELIGIBLE"
    # The byte-owner delivery-proof path is inert for a non-byte-owner (fail-closed).
    coherence_row = {
        "profile_member": "GT_SS_COHERENCE_V2", "layer": "detect.coherence",
        "outcome": "delivered", "chars_delivered": 12, "content_sha256_16": "a" * 16,
    }
    assert metrics._member_delivery_byte_proven(
        "GT_SS_COHERENCE_V2", [coherence_row],
        {"entries": [_seal_entry("detect.coherence")]},
    ) is False
    # Its control terminal is reachable and, absent any control.participation row, honestly
    # UNMEASURED (fail-closed) — never a fabricated pass.
    readiness = metrics._infra_control_readiness(
        "GT_SS_COHERENCE_V2", ("recovery",), _fact_lifecycles(),
        ledger_artifact="ledger", control_evidence={"records": {}, "joins": {}, "correctness": {}},
    )
    assert set(readiness["gates"]) == {
        "runtime_member_control_receipt", "mediated_fact_ids", "mediation_correct",
    }
    assert readiness["gates"]["mediation_correct"] is None
    assert readiness["infra_control_complete"] is False
    assert readiness["mediation_causal_fair_probe"] is None
