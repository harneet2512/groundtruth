from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_feature_metrics as metrics  # noqa: E402


PAYLOAD = "\ncheck src/widget.py before submit"
CANDIDATE_ID = "candidate:submit:1"


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _delivery(**changes: object) -> dict:
    row = {
        "layer": "submit_gate",
        "event_type": "submit_refusal",
        "outcome": "delivered",
        "iteration": 7,
        "chars_delivered": len(PAYLOAD),
        "content_sha256_16": _sha16(PAYLOAD),
        "native_text": PAYLOAD,
        "candidate_id": CANDIDATE_ID,
        "lineage_schema": "gt.feature_lineage.v1",
        "evidence_type": "submit_refusal",
        "runtime_producer_id": "submit_gate",
        "registered_producer_id": "submit_gate",
        "producer_registration_match": True,
        "fact_class": "submit_refusal",
    }
    row.update(changes)
    return row


def _ack(*, decision: str = "APPLIED", **changes: object) -> dict:
    row = {
        "schema": "gt.control_participation.v1",
        "layer": "control.participation",
        "event_type": "control_decision",
        "outcome": "evaluated",
        "chars_delivered": 0,
        "iteration": 9,
        "participation_decision": decision,
        "control_ref": {
            "category": "CAP",
            "feature_id": "GT_SS_ACK_METRICS",
            "role": "mediator",
        },
        "decision_site": "mini_seam.acknowledgment.receipt_grading",
        "temporal_relation": "RECEIPT_FOLLOWS_DELIVERY",
        "related_delivery_iteration": 7,
        "candidate_chars": len(PAYLOAD),
        "candidate_sha256_16": _sha16(PAYLOAD),
        "fact_class": "submit_refusal",
        "candidate_id": CANDIDATE_ID,
        "reason": (
            "later_assistant_acknowledgment_observed"
            if decision == "APPLIED"
            else "receipt_window_expired_without_acknowledgment"
        ),
    }
    row.update(changes)
    return row


def _consumption(receipt: int) -> dict:
    return {
        "entries": [{
            "source": "trajectory",
            "joined": True,
            "join_method": "seal",
            "content_sha256_16": _sha16(PAYLOAD),
            "ledger_chars": len(PAYLOAD),
            "ledger_layer": "submit_gate",
            "receipt": receipt,
            "msg_index": 0,
            "referenced_msg_index": 1 if receipt >= 2 else None,
            "acted_msg_index": 2 if receipt >= 3 else None,
        }],
    }


def _evidence(rows: list[dict], receipt: int = 2) -> dict:
    messages = [
        {"role": "tool", "content": PAYLOAD},
        {"role": "assistant", "content": "I will address src/widget.py."},
    ]
    return metrics._control_participation_evidence(
        rows, messages, _consumption(receipt),
    )


def test_ack_control_joins_exact_preceding_delivery_to_later_receipt() -> None:
    evidence = _evidence([_delivery(), _ack()])

    assert evidence["valid"] is True
    join = evidence["joins"]["GT_SS_ACK_METRICS"][0]
    assert join["delivery_row_index"] == 0
    assert join["row_index"] == 1
    assert join["temporal_relation"] == "RECEIPT_FOLLOWS_DELIVERY"
    assert join["delivery_iteration"] == 7
    assert join["iteration"] == 9
    assert join["receipt_level"] == 2
    assert join["referenced_message_index"] == 1
    assert join["candidate_id"] == CANDIDATE_ID
    assert "causal" not in repr(join).lower()


@pytest.mark.parametrize(
    "rows,receipt",
    [
        ([_delivery(), _ack(iteration=7)], 2),
        ([_delivery(), _ack(candidate_id="candidate:other")], 2),
        ([_delivery(), _ack(candidate_sha256_16="0" * 16)], 2),
        ([_delivery(iteration=6), _ack()], 2),
        ([_ack(), _delivery()], 2),
        ([_delivery(), _ack()], 1),
        ([_delivery(), _ack(decision="NO_EFFECT")], 0),
    ],
)
def test_ack_control_fails_closed_without_exact_strictly_later_receipt(
    rows: list[dict], receipt: int,
) -> None:
    evidence = _evidence(rows, receipt)
    assert evidence.get("joins", {}).get("GT_SS_ACK_METRICS", []) == []


def test_ack_expiry_no_effect_joins_only_when_receipt_stayed_below_two() -> None:
    no_effect = _evidence([_delivery(), _ack(decision="NO_EFFECT")], receipt=1)
    join = no_effect["joins"]["GT_SS_ACK_METRICS"][0]
    assert join["decision"] == "NO_EFFECT"
    assert join["receipt_level"] == 1

    contradicted = _evidence(
        [_delivery(), _ack(decision="NO_EFFECT")], receipt=2,
    )
    assert contradicted.get("joins", {}).get("GT_SS_ACK_METRICS", []) == []


def test_pre_delivery_mediator_cannot_claim_post_delivery_temporal_relation() -> None:
    row = _ack()
    row["control_ref"] = {
        "category": "CAP", "feature_id": "GT_CONTRACT_NATIVE", "role": "mediator",
    }
    row["decision_site"] = "mini_seam.contract.native_render"
    row["fact_class"] = "caller_contract"

    evidence = _evidence([row])
    assert evidence["valid"] is False
    assert evidence["invalid_rows"] == [0]
