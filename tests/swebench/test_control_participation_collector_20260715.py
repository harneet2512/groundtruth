from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_feature_metrics as metrics  # noqa: E402


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _participation(payload: str, **changes: object) -> dict:
    row = {
        "schema": "gt.control_participation.v1",
        "layer": "control.participation",
        "event_type": "control_decision",
        "outcome": "evaluated",
        "chars_delivered": 0,
        "iteration": 4,
        "participation_decision": "APPLIED",
        "control_ref": {
            "category": "CAP", "feature_id": "GT_CONTRACT_NATIVE", "role": "mediator",
        },
        "decision_site": "mini_seam.contract.native_render",
        "candidate_chars": len(payload),
        "candidate_sha256_16": _sha16(payload),
        "fact_class": "caller_contract",
        "candidate_id": "contract:dedup-1",
        "reason": "native_render_selected",
    }
    row.update(changes)
    return row


def _delivered(payload: str, **changes: object) -> dict:
    row = {
        "layer": "l3.contract", "event_type": "post_view", "outcome": "delivered",
        "iteration": 4, "chars_delivered": len(payload),
        "content_sha256_16": _sha16(payload), "native_text": payload,
        "candidate_id": "contract:dedup-1",
        "lineage_schema": "gt.feature_lineage.v1",
        "evidence_type": "caller_contract",
        "runtime_producer_id": "contract_map",
        "registered_producer_id": "contract_map",
        "producer_registration_match": True,
        "fact_class": "caller_contract",
    }
    row.update(changes)
    return row


def _write_task(tmp_path, rows: list[dict], payload: str) -> str:
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(json.dumps({
        "trajectory_format": "mini-swe-agent",
        "messages": [{"role": "tool", "content": payload}],
        "info": {"submission": ""},
    }), encoding="utf-8")
    (tmp_path / "gt_runtime_ledger_synthetic.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return str(tmp_path)


def test_mediator_requires_exact_downstream_fact_and_observation_seal(tmp_path) -> None:
    payload = "src/mod.py:12 preserve caller contract"
    record = metrics.collect_task(
        "synthetic__control-1",
        _write_task(tmp_path, [_participation(payload), _delivered(payload)], payload),
    )
    readiness = record["features"]["GT_CONTRACT_NATIVE"]["ss_readiness"]

    assert readiness["gates"]["runtime_member_control_receipt"] is True
    assert readiness["gates"]["mediated_fact_ids"] is True
    assert readiness["gates"]["mediation_correct"] is None
    assert readiness["gates"]["mediation_causal_fair_probe"] is None
    assert readiness["mediation"]["runtime_linked_fact_ids"] == ["caller_contract"]
    join = readiness["mediation"]["participation_joins"][0]
    assert join["candidate_id"] == "contract:dedup-1"
    assert join["delivery_row_index"] == 1
    assert join["observation_joined"] is True
    assert join["receipt_level"] >= 1
    assert readiness["ss_live"] is False


def test_wrong_site_error_or_class_cooccurrence_cannot_admit_control(tmp_path) -> None:
    payload = "src/mod.py:12 preserve caller contract"
    bad = _participation(payload, decision_site="invented.site")
    error = _participation(payload, participation_decision="ERROR", outcome="measurement_failed")
    record = metrics.collect_task(
        "synthetic__control-2",
        _write_task(tmp_path, [bad, error, _delivered(payload)], payload),
    )
    readiness = record["features"]["GT_CONTRACT_NATIVE"]["ss_readiness"]

    assert readiness["gates"]["runtime_member_control_receipt"] is None
    assert readiness["mediation"]["runtime_linked_fact_ids"] == []
    assert record["integrity"]["control_participation_valid"] is False
    assert record["integrity"]["control_participation_invalid_rows"] == [0, 1]


def test_candidate_id_and_exact_seal_are_both_load_bearing(tmp_path) -> None:
    payload = "src/mod.py:12 preserve caller contract"
    for index, participation in enumerate((
        _participation(payload, candidate_id=""),
        _participation(payload, candidate_sha256_16="0" * 16),
    )):
        case = tmp_path / str(index)
        case.mkdir()
        record = metrics.collect_task(
            f"synthetic__control-{index + 3}",
            _write_task(case, [participation, _delivered(payload)], payload),
        )
        readiness = record["features"]["GT_CONTRACT_NATIVE"]["ss_readiness"]
        assert readiness["gates"]["runtime_member_control_receipt"] is None
        assert readiness["mediation"]["participation_joins"] == []


def test_zero_candidate_no_effect_is_valid_but_never_joined(tmp_path) -> None:
    payload = "src/mod.py:12 preserve caller contract"
    participation = _participation(
        payload,
        participation_decision="NO_EFFECT",
        candidate_chars=0,
        candidate_sha256_16="",
    )
    record = metrics.collect_task(
        "synthetic__control-zero-no-effect",
        _write_task(tmp_path, [participation, _delivered(payload)], payload),
    )
    readiness = record["features"]["GT_CONTRACT_NATIVE"]["ss_readiness"]

    assert record["integrity"]["control_participation_valid"] is True
    assert len(readiness["mediation"]["participation_records"]) == 1
    assert readiness["mediation"]["participation_joins"] == []
    assert readiness["gates"]["runtime_member_control_receipt"] is None


def test_delivery_without_exact_candidate_id_cannot_join(tmp_path) -> None:
    payload = "src/mod.py:12 preserve caller contract"
    delivery = _delivered(payload)
    delivery.pop("candidate_id")
    record = metrics.collect_task(
        "synthetic__control-no-downstream-id",
        _write_task(tmp_path, [_participation(payload), delivery], payload),
    )
    readiness = record["features"]["GT_CONTRACT_NATIVE"]["ss_readiness"]

    assert record["integrity"]["control_participation_valid"] is True
    assert readiness["mediation"]["participation_joins"] == []
    assert readiness["gates"]["runtime_member_control_receipt"] is None


def test_layer_alias_cannot_override_wrong_typed_fact_lineage(tmp_path) -> None:
    payload = "src/mod.py:12 preserve caller contract"
    delivery = _delivered(payload, fact_class="recovery")
    record = metrics.collect_task(
        "synthetic__control-wrong-typed-fact",
        _write_task(tmp_path, [_participation(payload), delivery], payload),
    )
    readiness = record["features"]["GT_CONTRACT_NATIVE"]["ss_readiness"]

    assert record["integrity"]["control_participation_valid"] is True
    assert readiness["mediation"]["participation_joins"] == []
    assert readiness["gates"]["runtime_member_control_receipt"] is None


def test_authorized_fine_grained_runtime_producer_can_join(tmp_path) -> None:
    payload = "src/mod.py:12 preserve caller contract"
    delivery = _delivered(
        payload,
        evidence_type="caller_break",
        runtime_producer_id="caller_contract",
        registered_producer_id="contract_map",
    )
    record = metrics.collect_task(
        "synthetic__control-fine-producer",
        _write_task(tmp_path, [_participation(payload), delivery], payload),
    )
    readiness = record["features"]["GT_CONTRACT_NATIVE"]["ss_readiness"]
    assert len(readiness["mediation"]["participation_joins"]) == 1


def test_unregistered_runtime_producer_cannot_join_even_with_true_claim(tmp_path) -> None:
    payload = "src/mod.py:12 preserve caller contract"
    delivery = _delivered(
        payload,
        evidence_type="caller_break",
        runtime_producer_id="invented_producer",
        registered_producer_id="contract_map",
    )
    record = metrics.collect_task(
        "synthetic__control-bad-producer",
        _write_task(tmp_path, [_participation(payload), delivery], payload),
    )
    readiness = record["features"]["GT_CONTRACT_NATIVE"]["ss_readiness"]
    assert readiness["mediation"]["participation_joins"] == []


def test_nonzero_no_effect_candidate_can_join_but_suppressed_cannot(tmp_path) -> None:
    payload = "src/mod.py:12 preserve caller contract"
    no_effect = _participation(payload, participation_decision="NO_EFFECT")
    record = metrics.collect_task(
        "synthetic__control-preserved",
        _write_task(tmp_path, [no_effect, _delivered(payload)], payload),
    )
    joins = record["features"]["GT_CONTRACT_NATIVE"]["ss_readiness"]["mediation"]["participation_joins"]
    assert len(joins) == 1
    assert joins[0]["decision"] == "NO_EFFECT"

    suppressed_dir = tmp_path / "suppressed"
    suppressed_dir.mkdir()
    suppressed = _participation(payload, participation_decision="SUPPRESSED", reason="lost")
    suppressed_record = metrics.collect_task(
        "synthetic__control-suppressed",
        _write_task(suppressed_dir, [suppressed, _delivered(payload)], payload),
    )
    assert suppressed_record["features"]["GT_CONTRACT_NATIVE"]["ss_readiness"]["mediation"][
        "participation_joins"
    ] == []


def test_legacy_layer_and_seal_cannot_byte_prove_fact_without_typed_lineage() -> None:
    payload = "src/mod.py:12 preserve caller contract"
    legacy = _delivered(payload)
    for field in (
        "lineage_schema", "evidence_type", "runtime_producer_id",
        "registered_producer_id", "producer_registration_match", "fact_class",
    ):
        legacy.pop(field)
    consumption = {
        "entries": [{
            "source": "trajectory", "joined": True, "join_method": "seal",
            "content_sha256_16": _sha16(payload), "ledger_chars": len(payload),
            "ledger_layer": "l3.contract",
        }],
    }
    assert metrics._fact_delivery_byte_proven("caller_contract", [legacy], consumption) is False


def _brief_control_fixture() -> tuple[dict, list[dict], dict]:
    brief = "Plan\n- preserve the stated obligation\n"
    start, end = 5, len(brief)
    block = brief[start:end]
    candidate_id = "obligations:brief"
    payload = {
        "schema": "gt.brief_result.v1",
        "brief_text": brief,
        "brief_sha256": hashlib.sha256(brief.encode()).hexdigest(),
        "metrics": {
            "block_receipts": [{
                "block_id": "obligations",
                "candidate_id": candidate_id,
                "fact_class": "obligations",
                "label": "obligations",
                "char_span": [start, end],
                "content_hash": hashlib.sha256(block.encode()).hexdigest(),
            }],
            "control_participation": [{
                "schema": "gt.control_participation.v1",
                "control_ref": {
                    "category": "CAP", "feature_id": "GT_BRIEF_NATIVE", "role": "mediator",
                },
                "decision_site": "pretask.v1r_brief.native_render",
                "decision": "APPLIED",
                "iteration": 0,
                "candidate_chars": len(block),
                "candidate_sha256_16": _sha16(block),
                "fact_class": "obligations",
                "candidate_id": candidate_id,
                "reason": "native_brief_rendered",
            }],
        },
    }
    messages = [{"role": "tool", "content": brief}]
    consumption = {
        "entries": [{
            "source": "trajectory", "joined": True, "join_method": "seal",
            "rendered_text": brief, "content_sha256_16": _sha16(brief),
            "chars": len(brief), "ledger_chars": len(brief),
            "ledger_layer": "l0.brief", "msg_index": 0, "receipt": 1,
        }],
    }
    return payload, messages, consumption


def test_pretask_control_joins_block_identity_to_parent_brief_observation() -> None:
    brief, messages, consumption = _brief_control_fixture()
    evidence = metrics._control_participation_evidence([], messages, consumption, brief)
    joins = evidence["joins"]["GT_BRIEF_NATIVE"]
    assert evidence["valid"] is True
    assert len(joins) == 1
    assert joins[0]["candidate_id"] == "obligations:brief"
    assert joins[0]["delivery_layer"] == "l0.brief"
    assert joins[0]["observation_message_index"] == 0


def test_parent_receipt_from_another_block_does_not_ack_target_control() -> None:
    brief = "Brief\n1. src/target.py (target)\n2. src/other.py (other)\n"
    target = "1. src/target.py (target)\n"
    start = brief.index(target)
    candidate_id = "localization:src/target.py"
    payload = {
        "schema": "gt.brief_result.v1", "brief_text": brief,
        "brief_sha256": hashlib.sha256(brief.encode()).hexdigest(),
        "metrics": {
            "block_receipts": [{
                "block_id": "file-entry-1", "candidate_id": candidate_id,
                "fact_class": "localization", "label": "file-entry-1",
                "char_span": [start, start + len(target)],
                "content_hash": hashlib.sha256(target.encode()).hexdigest(),
            }],
            "control_participation": [{
                "schema": "gt.control_participation.v1",
                "control_ref": {"category": "CAP", "feature_id": "GT_SEM_BODY", "role": "mediator"},
                "decision_site": "pretask.v1r_brief.semantic_body_rank",
                "decision": "APPLIED", "iteration": 0,
                "candidate_chars": len(target), "candidate_sha256_16": _sha16(target),
                "fact_class": "localization", "candidate_id": candidate_id, "reason": "ranked",
            }],
        },
    }
    messages = [
        {"role": "tool", "content": brief},
        {"role": "assistant", "content": "I will inspect src/other.py", "tool_calls": [{
            "function": {"name": "shell", "arguments": '{"command":"cat src/other.py"}'},
        }]},
    ]
    consumption = {"entries": [{
        "source": "trajectory", "joined": True, "join_method": "seal",
        "rendered_text": brief, "content_sha256_16": _sha16(brief),
        "chars": len(brief), "ledger_chars": len(brief), "ledger_layer": "l0.brief",
        "msg_index": 0, "receipt": 3,
    }]}
    join = metrics._control_participation_evidence([], messages, consumption, payload)["joins"][
        "GT_SEM_BODY"
    ][0]
    assert join["receipt_level"] == 1
    assert join["referenced_message_index"] is None
    assert join["acted_message_index"] is None


@pytest.mark.parametrize("failure", ["missing_parent", "bad_span_hash", "bad_id", "absent_observation"])
def test_pretask_control_fails_closed_on_broken_parent_or_block_chain(failure: str) -> None:
    brief, messages, consumption = _brief_control_fixture()
    if failure == "missing_parent":
        start, end = brief["metrics"]["block_receipts"][0]["char_span"]
        block = brief["brief_text"][start:end]
        consumption["entries"][0].update({
            "rendered_text": block,
            "content_sha256_16": _sha16(block),
            "chars": len(block),
            "ledger_chars": len(block),
        })
    elif failure == "bad_span_hash":
        brief["metrics"]["block_receipts"][0]["content_hash"] = "f" * 64
    elif failure == "bad_id":
        brief["metrics"]["control_participation"][0]["candidate_id"] = "unknown:block"
    else:
        messages = []
    evidence = metrics._control_participation_evidence([], messages, consumption, brief)
    assert evidence.get("joins", {}).get("GT_BRIEF_NATIVE", []) == []
    if failure in {"bad_span_hash", "bad_id"}:
        assert evidence["valid"] is False
