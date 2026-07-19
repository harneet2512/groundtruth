from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from groundtruth.runtime.adapters.miniswe import seal_delivery
from groundtruth.runtime.evidence_envelope import (
    EVENT_SEARCH,
    RECEIPT_ACTED,
    EvidenceEnvelope,
    build_observation_binding,
    to_dict,
)
from groundtruth.runtime.feature_lineage import build_lineage, lineage_to_dict
from scripts.swebench.receipt_sidecar import (
    COLLECTION_INTEGRITY_SCHEMA,
    canonical_receipt_key,
    join_receipt_evidence,
    load_receipt_sidecar,
    sealed_receipt_expected,
)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swebench"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import gt_feature_metrics as metrics  # noqa: E402


def _receipt_rows() -> list[dict]:
    parent = "1" * 64
    action = "2" * 64
    lineage = build_lineage(
        runtime_producer_id="def_ref_partition",
        evidence_type="def_ref_partition",
        actual_event="search_result",
    )
    assert lineage is not None
    env = EvidenceEnvelope.build(
        producer="def_ref_partition",
        fact_id="Widget",
        target="src/widget.py",
        evidence_type="def_ref_partition",
        payload=("Widget is defined here",),
        provenance=(("src/widget.py", 12),),
        confidence=0.6,
        preferred_event=EVENT_SEARCH,
        lineage=lineage,
    )
    binding = build_observation_binding(
        batch_start_iteration=3,
        parent_policy_sha256=parent,
        parent_policy_chars=120,
        action_batch_sha256=action,
        candidate_ordinal=0,
        candidate_kind="def_ref_partition",
        candidate_id=env.dedup_key,
    )
    sealed, _ = seal_delivery(
        env,
        episode_id="episode",
        event_id="3",
        parent_hash="",
        rendered_bytes=b"Widget is defined here",
        renderer_id="gateway",
        observation_binding=binding,
    )
    acted = dataclasses.replace(sealed, receipt_state=RECEIPT_ACTED)
    return [
        {
            "schema": "gt_receipt.v1",
            "kind": "gateway",
            "transition": "delivered",
            "action_index": 3,
            "ts_ms": 100,
            "envelope": to_dict(sealed),
            "lineage": lineage_to_dict(lineage),
        },
        {
            "schema": "gt_receipt.v1",
            "kind": "gateway",
            "transition": "acted",
            "action_index": 4,
            "ts_ms": 101,
            "envelope": to_dict(acted),
            "lineage": lineage_to_dict(lineage),
        },
    ]


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_valid_ladder_requires_trajectory_corroboration_for_ack(tmp_path: Path) -> None:
    path = tmp_path / "gt_receipts_task.jsonl"
    _write(path, _receipt_rows())

    sidecar = load_receipt_sidecar(path, sealed_delivery_expected=True)
    assert sidecar.integrity_ok
    assert len(sidecar.records) == 2
    key = sidecar.records[0].key

    telemetry_only = join_receipt_evidence(
        sidecar, key, trajectory_corroborated=False
    )
    assert telemetry_only.matched
    assert telemetry_only.sidecar_transition == "acted"
    assert not telemetry_only.acknowledgment_supported

    corroborated = join_receipt_evidence(
        sidecar, key, trajectory_corroborated=True
    )
    assert corroborated.acknowledgment_supported


def test_missing_sidecar_is_failure_only_when_sealed_delivery_expected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.jsonl"
    quiet = load_receipt_sidecar(path, sealed_delivery_expected=False)
    assert quiet.integrity_ok
    assert not quiet.source_present

    failed = load_receipt_sidecar(path, sealed_delivery_expected=True)
    assert not failed.integrity_ok
    assert [failure.code for failure in failed.failures] == [
        "receipt_sidecar_missing_for_sealed_delivery"
    ]


def test_receipt_expectation_excludes_brief_and_requires_bound_envelope() -> None:
    rows = _receipt_rows()
    envelope = rows[0]["envelope"]
    binding = envelope["observation_binding"]
    physical = {
        "layer": "gateway.def_ref_partition",
        "outcome": "delivered",
        "chars_delivered": 24,
        "content_sha256_16": envelope["rendered_bytes_hash"][:16],
        "candidate_id": binding["candidate_id"],
        "observation_binding": binding,
    }
    brief = dict(physical, layer="brief.task")
    unbound = dict(physical)
    unbound.pop("observation_binding")

    assert not sealed_receipt_expected((brief,))
    assert not sealed_receipt_expected((unbound,))
    assert sealed_receipt_expected((physical,))


def test_rejects_nonmonotone_transition_and_identity_change(tmp_path: Path) -> None:
    rows = _receipt_rows()
    rows.append(dict(rows[1]))
    rows[2]["action_index"] = 2
    rows[2]["ts_ms"] = 102
    rows[2]["kind"] = "lane"
    path = tmp_path / "gt_receipts_task.jsonl"
    _write(path, rows)

    sidecar = load_receipt_sidecar(path, sealed_delivery_expected=True)
    codes = {failure.code for failure in sidecar.failures}
    assert "receipt_transition_not_strictly_monotone" in codes
    assert "receipt_action_index_regressed" in codes
    assert "receipt_kind_changed" in codes


def test_rejects_envelope_candidate_binding_mismatch(tmp_path: Path) -> None:
    rows = _receipt_rows()
    envelope = rows[0]["envelope"]
    envelope["observation_binding"] = dict(envelope["observation_binding"])
    envelope["observation_binding"]["candidate_id"] = "f" * 64
    path = tmp_path / "gt_receipts_task.jsonl"
    _write(path, rows[:1])

    sidecar = load_receipt_sidecar(path, sealed_delivery_expected=True)
    assert not sidecar.integrity_ok
    assert sidecar.failures[0].code == "receipt_envelope_from_dict_invalid"


def test_rejects_invalid_typed_lineage(tmp_path: Path) -> None:
    rows = _receipt_rows()
    rows[0]["lineage"] = dict(rows[0]["lineage"])
    rows[0]["lineage"]["producer_registration_match"] = False
    path = tmp_path / "gt_receipts_task.jsonl"
    _write(path, rows[:1])

    sidecar = load_receipt_sidecar(path, sealed_delivery_expected=True)
    assert not sidecar.integrity_ok
    assert sidecar.failures[0].code == "receipt_lineage_unregistered_producer"


def test_rejects_forged_serialized_registry_claims(tmp_path: Path) -> None:
    rows = _receipt_rows()
    for row in rows:
        row["lineage"] = dict(row["lineage"])
        row["lineage"]["registered_producer_id"] = "forged"
        row["lineage"]["required_event"] = "task_start"
        row["lineage"]["receipt_predicate"] = "forged_predicate"
    path = tmp_path / "gt_receipts_task.jsonl"
    _write(path, rows)

    sidecar = load_receipt_sidecar(path, sealed_delivery_expected=True)
    assert not sidecar.integrity_ok
    assert sidecar.failures[0].code == "receipt_lineage_registry_mismatch"


def test_collection_failure_marker_invalidates_partial_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "gt_receipts_task.jsonl"
    marker = tmp_path / "gt_receipt_integrity_task.json"
    _write(path, _receipt_rows())
    marker.write_text(
        json.dumps(
            {
                "schema": COLLECTION_INTEGRITY_SCHEMA,
                "status": "FAIL",
                "reason": "receipt_sidecar_collection_copy_failed",
            }
        ),
        encoding="utf-8",
    )

    sidecar = load_receipt_sidecar(
        path,
        sealed_delivery_expected=True,
        collection_integrity_paths=(marker,),
    )
    assert not sidecar.integrity_ok
    assert sidecar.failures[0].code == "receipt_sidecar_collection_copy_failed"
    key = canonical_receipt_key(
        content_sha256_16=sidecar.records[0].key.content_sha256_16,
        candidate_id=sidecar.records[0].key.candidate_id,
        observation_id=sidecar.records[0].key.observation_id,
        opportunity_id=sidecar.records[0].key.opportunity_id,
    )
    result = join_receipt_evidence(sidecar, key, trajectory_corroborated=True)
    assert not result.integrity_ok
    assert not result.acknowledgment_supported


def test_feature_metrics_requires_exact_sidecar_and_trajectory_ack(
    tmp_path: Path,
) -> None:
    receipt_rows = _receipt_rows()
    envelope = receipt_rows[0]["envelope"]
    binding = envelope["observation_binding"]
    ledger_rows = [{
        "layer": "gateway.def_ref_partition",
        "outcome": "delivered",
        "chars_delivered": 24,
        "content_sha256_16": envelope["rendered_bytes_hash"][:16],
        "candidate_id": binding["candidate_id"],
        "observation_binding": binding,
    }]
    chronology = SimpleNamespace(
        fact_class="def_partition",
        evidence_type="def_ref_partition",
        ledger_row_index=0,
        chronology=SimpleNamespace(
            delivery_index=2,
            decision_open_index=1,
            decision_commit_index=3,
            native_acquisition_index=None,
        ),
    )
    sidecar_path = tmp_path / "gt_receipts_task.jsonl"
    _write(sidecar_path, receipt_rows)

    acknowledged, diagnostics = metrics._receipt_corroborated_acknowledgment(
        "task",
        str(tmp_path),
        ledger_rows,
        [chronology],
        {"def_partition": True},
        messages=[],
        attestations=(),
    )
    assert diagnostics["integrity_ok"] is True
    assert acknowledged["def_partition"] is True

    sidecar_path.unlink()
    unmeasured, failed = metrics._receipt_corroborated_acknowledgment(
        "task",
        str(tmp_path),
        ledger_rows,
        [chronology],
        {"def_partition": True},
        messages=[],
        attestations=(),
    )
    assert failed["integrity_ok"] is False
    assert unmeasured["def_partition"] is None


def test_feature_metrics_rejects_unjoined_expected_delivery_row(
    tmp_path: Path,
) -> None:
    receipt_rows = _receipt_rows()
    envelope = receipt_rows[0]["envelope"]
    binding = envelope["observation_binding"]
    ledger_row = {
        "layer": "gateway.def_ref_partition",
        "outcome": "delivered",
        "chars_delivered": 24,
        "content_sha256_16": envelope["rendered_bytes_hash"][:16],
        "candidate_id": binding["candidate_id"],
        "observation_binding": binding,
    }
    chronology = SimpleNamespace(
        fact_class="def_partition",
        evidence_type="def_ref_partition",
        ledger_row_index=0,
        chronology=SimpleNamespace(
            delivery_index=2,
            decision_open_index=1,
            decision_commit_index=3,
            native_acquisition_index=None,
        ),
    )
    _write(tmp_path / "gt_receipts_task.jsonl", receipt_rows)
    _, diagnostics = metrics._receipt_corroborated_acknowledgment(
        "task",
        str(tmp_path),
        [ledger_row, dict(ledger_row)],
        [chronology],
        {"def_partition": True},
        messages=[],
        attestations=(),
    )
    assert diagnostics["integrity_ok"] is False
    assert diagnostics["missing_expected_ledger_row_indices"] == [1]
