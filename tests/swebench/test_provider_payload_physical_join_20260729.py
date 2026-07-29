"""#43 — the BYTE-JOIN 0/80 wall: the capsule is model-visible at the PROVIDER
boundary, and no trajectory-joined reader could see it.

WHAT WAS BROKEN.  ``miniswe_provider_boundary._install.prepare`` (:1300-1317)
appends the capsule message to the *prepared provider payload*, never to
``agent.messages``.  The trajectory file therefore never contains the capsule
bytes, so ``consumption_ledger._build_v2``'s seal join (which searches only
trajectory messages) could not match a single canonical capsule row.  Every one
became ``source="ledger_only"`` -> ``physical_delivery_authority`` reported
BROKEN_PHYSICAL_BINDING -> ``gt_behavioral_impact`` (:183-190, counts only
``source=="trajectory"`` AND ``joined is True``) reported
``total_deliveries=0`` BY CONSTRUCTION, on runs where real deliveries flowed.

THE FIX (reader-side, direction (a)).  The canonical delivery row already
carries ``bound_provider_payload_json`` — the EXACT bytes dispatched to the
provider — plus ``provider_payload_hash`` and a ``capsule_binding`` naming the
message/content coordinates of the capsule inside it.  That is a second
PHYSICAL substrate for model visibility, and it proves itself:

  sha256(bound_provider_payload_json) == provider_payload_hash
  payload["messages"][message_index]["content"][content_index]["text"] == capsule_text
  sha256(capsule_text) == rendered_content_hash, [:16] == content_sha256_16
  chars_delivered == len(capsule_text)

The trajectory side of the join is anchored by the payload message immediately
preceding the capsule: its exact text must occur in this trajectory.  A join has
TWO sides — without that anchor a payload from another task could seat receipts
in this trajectory, and the receipt ladder would have no ordering.

FAIL-CLOSED.  Every rejection carries a NAMED reason on the ledger_only entry
and on the physical-delivery authority record.  An unjoinable delivery stays
UNMEASURED; it is never fabricated into a receipt.

BITING MUTATIONS (each applied, observed RED, reverted):
  M1 — drop the ``sha256(payload_json) == provider_payload_hash`` check:
       ``test_a_tampered_payload_body_is_unmeasured`` goes GREEN-when-it-must-be-RED,
       i.e. an edited payload would seat a receipt.
  M2 — accept the capsule anywhere in the payload instead of at the BOUND
       coordinates: ``test_a_capsule_not_at_its_bound_location_is_unmeasured``.
  M3 — drop the trajectory context anchor: ``test_an_unanchored_payload_is_unmeasured``
       — a foreign payload would join into this trajectory.
  M4 — try the payload join BEFORE the trajectory seal join:
       ``test_the_trajectory_seal_join_still_wins_when_the_bytes_are_visible``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import consumption_ledger as cl  # noqa: E402
import gt_behavioral_impact as gbi  # noqa: E402


CAPSULE = (
    "[VERIFIED] localization: src/pkg/mod.py defines resolve_alias\n"
    "the alias table must stay ordered\n"
)
OBSERVATION = "OBS-1\nsrc/pkg/other.py\nsrc/pkg/third.py\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _trajectory() -> list[dict]:
    """A mini-swe trajectory that NEVER contains the capsule bytes."""
    return [
        {"role": "system", "content": "you are an agent"},
        {"role": "user", "content": "fix the alias resolution bug"},
        {
            "role": "assistant",
            "content": "I will look for the alias table.",
            "extra": {"actions": [{"command": "grep -rn alias src/"}]},
        },
        {"role": "tool", "content": OBSERVATION},
        {
            "role": "assistant",
            "content": "src/pkg/mod.py is the edit target; patching it now.",
            "extra": {
                "actions": [
                    {"command": "sed -i 's/old/new/' src/pkg/mod.py"},
                ]
            },
        },
        {"role": "tool", "content": "patch applied"},
    ]


def _payload_messages(capsule: str = CAPSULE) -> list[dict]:
    """``_prepare_messages_for_api(agent.messages)`` + the appended capsule."""
    return [
        {"role": "system", "content": [{"type": "text", "text": "you are an agent"}]},
        {"role": "user", "content": [{"type": "text", "text": "fix the alias resolution bug"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "I will look for the alias table."}]},
        {"role": "tool", "content": [{"type": "text", "text": OBSERVATION}]},
        {"role": "user", "content": [{"type": "text", "text": capsule}]},
    ]


def _canonical_row(
    *,
    capsule: str = CAPSULE,
    payload_messages: list[dict] | None = None,
    message_index: int = 4,
    content_index: int = 0,
    payload_hash: str | None = None,
    drop_payload: bool = False,
) -> dict:
    messages = _payload_messages() if payload_messages is None else payload_messages
    payload_json = json.dumps(
        {"model": "test-model", "messages": messages},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    rendered = _sha(capsule)
    row = {
        "schema": "gt.canonical_delivery.v1",
        "layer": "canonical.provider_delivery",
        "event_type": "canonical_provider_delivery",
        "outcome": "delivered",
        "delivery_attempt_id": "da-1",
        "capsule_text": capsule,
        "rendered_content_hash": rendered,
        "content_sha256_16": rendered[:16],
        "chars_delivered": len(capsule),
        "provider_payload_hash": payload_hash or _sha(payload_json),
        "bound_provider_payload_json": payload_json,
        "capsule_binding": {
            "schema": "gt.capsule_binding.v1",
            "provider_payload_hash": payload_hash or _sha(payload_json),
            "message_index": message_index,
            "content_index": content_index,
        },
    }
    if drop_payload:
        row.pop("bound_provider_payload_json")
    return row


def _ledger(rows: list[dict], trajectory: list[dict] | None = None) -> dict:
    return cl.build_consumption_ledger(
        {"messages": _trajectory() if trajectory is None else trajectory},
        runtime_ledger_rows=rows,
    )


def _payload_entries(ledger: dict) -> list[dict]:
    return [
        e for e in ledger["entries"]
        if e.get("source") == cl.PROVIDER_PAYLOAD_SOURCE
    ]


def _ledger_only(ledger: dict) -> list[dict]:
    return [e for e in ledger["entries"] if e.get("source") == "ledger_only"]


# ---------------------------------------------------------------------- #
# CALIBRATION — without this every "unmeasured" assertion below is unreadable.
# ---------------------------------------------------------------------- #
def test_the_capsule_bytes_are_absent_from_the_trajectory() -> None:
    """The premise. If the capsule leaked into the trajectory this whole file
    would be testing the seal join instead."""
    blob = json.dumps(_trajectory())
    assert CAPSULE not in blob


def test_a_provider_payload_delivery_joins_with_source_and_identity() -> None:
    ledger = _ledger([_canonical_row()])
    entries = _payload_entries(ledger)
    assert len(entries) == 1, ledger["entries"]
    entry = entries[0]
    assert entry["joined"] is True
    assert entry["join_method"] == cl.PROVIDER_PAYLOAD_JOIN_METHOD
    assert entry["physical_substrate"] == cl.PROVIDER_PAYLOAD_SOURCE
    assert entry["content_sha256_16"] == _sha(CAPSULE)[:16]
    assert entry["rendered_text"] == CAPSULE
    assert entry["chars"] == len(CAPSULE)
    assert entry["ledger_layer"] == "canonical.provider_delivery"
    assert entry["physical_id"].startswith("pp0:m4:c0")
    # anchored at the tool observation that preceded the model call
    assert entry["msg_index"] == 3
    assert ledger["ledger_rows_delivered"] == 1
    assert ledger["ledger_rows_joined"] == 1
    assert ledger["join_rate"] == 1.0
    assert ledger["gt_blocks_delivered"] == 1


def test_the_receipt_ladder_runs_on_the_anchored_trajectory() -> None:
    """Delivered -> referenced -> acted, from the assistant turns that FOLLOW
    the anchor. The capsule names src/pkg/mod.py; the next assistant message
    names it in prose (2) and sed -i's it (3)."""
    entry = _payload_entries(_ledger([_canonical_row()]))[0]
    assert entry["receipt"] == 3
    assert entry["referenced_msg_index"] == 4
    assert entry["acted_msg_index"] == 4


def test_the_physical_delivery_authority_binds_the_payload_delivery() -> None:
    authority = cl.physical_delivery_authority(_ledger([_canonical_row()]))
    assert authority["valid"] is True
    record = authority["deliveries"]["0"]
    assert record["state"] == cl.PHYSICAL_DELIVERY_BOUND
    assert record["physical_substrate"] == cl.PROVIDER_PAYLOAD_SOURCE
    assert record["content_sha256_16"] == _sha(CAPSULE)[:16]
    assert record["receipt"] == 3


def test_the_behavioral_impact_denominator_is_no_longer_zero() -> None:
    """THE RECORDED WALL. total_deliveries=0 BY CONSTRUCTION was the defect."""
    ledger = _ledger([_canonical_row()])
    impact = gbi.analyze_trajectory(
        {"messages": _trajectory()}, consumption_ledger=ledger
    )
    assert impact["summary"]["total_deliveries"] == 1
    assert impact["summary"]["gt_tokens_injected"] == len(CAPSULE)
    assert impact["summary"]["impact_rate_reason"] is None


# ---------------------------------------------------------------------- #
# FAIL-CLOSED — an unjoinable delivery stays UNMEASURED with a NAMED reason.
# ---------------------------------------------------------------------- #
def _assert_unmeasured(rows: list[dict], reason: str, trajectory=None) -> None:
    ledger = _ledger(rows, trajectory)
    assert _payload_entries(ledger) == []
    only = _ledger_only(ledger)
    assert len(only) == 1, ledger["entries"]
    assert only[0]["physical_join_reason"] == reason
    assert only[0]["receipt"] is None
    assert ledger["ledger_rows_joined"] == 0
    authority = cl.physical_delivery_authority(ledger)
    assert authority["valid"] is False
    assert authority["deliveries"]["0"]["state"] == cl.BROKEN_PHYSICAL_BINDING
    assert authority["deliveries"]["0"]["reason"] == reason
    impact = gbi.analyze_trajectory(
        {"messages": _trajectory() if trajectory is None else trajectory},
        consumption_ledger=ledger,
    )
    assert impact["summary"]["total_deliveries"] == 0
    assert impact["summary"]["impact_rate"] is None


def test_an_absent_payload_is_unmeasured_with_a_named_reason() -> None:
    _assert_unmeasured([_canonical_row(drop_payload=True)], "provider_payload_absent")


def test_a_tampered_payload_body_is_unmeasured() -> None:
    """M1. The recorded hash is the only thing standing between an offline
    reader and an edited payload."""
    row = _canonical_row()
    payload = json.loads(row["bound_provider_payload_json"])
    payload["messages"][3]["content"][0]["text"] = "a different observation"
    row["bound_provider_payload_json"] = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    _assert_unmeasured([row], "provider_payload_hash_mismatch")


def test_a_capsule_not_at_its_bound_location_is_unmeasured() -> None:
    """M2. Present-somewhere is not delivered-here."""
    messages = _payload_messages()
    messages[3]["content"][0]["text"] = CAPSULE  # capsule text, wrong slot
    messages[4]["content"][0]["text"] = "not the capsule"
    _assert_unmeasured(
        [_canonical_row(payload_messages=messages)],
        "provider_payload_capsule_not_at_bound_location",
    )


def test_an_out_of_range_binding_is_unmeasured() -> None:
    _assert_unmeasured(
        [_canonical_row(message_index=99)],
        "provider_payload_message_index_out_of_range",
    )
    _assert_unmeasured(
        [_canonical_row(content_index=7)],
        "provider_payload_content_index_out_of_range",
    )


def test_a_binding_that_names_another_payload_is_unmeasured() -> None:
    row = _canonical_row()
    row["capsule_binding"]["provider_payload_hash"] = "f" * 64
    _assert_unmeasured([row], "provider_payload_binding_mismatch")


def test_a_capsule_whose_seal_does_not_match_its_text_is_unmeasured() -> None:
    row = _canonical_row()
    row["content_sha256_16"] = "0" * 16
    _assert_unmeasured([row], "provider_payload_capsule_seal_mismatch")


def test_an_unanchored_payload_is_unmeasured() -> None:
    """M3. A payload whose context does not occur in THIS trajectory cannot
    seat receipts in it — the join has two sides."""
    messages = _payload_messages()
    messages[3]["content"][0]["text"] = "an observation from another task"
    _assert_unmeasured(
        [_canonical_row(payload_messages=messages)],
        "provider_payload_context_unanchored",
    )


def test_a_capsule_with_no_preceding_context_is_unmeasured() -> None:
    _assert_unmeasured(
        [_canonical_row(payload_messages=[_payload_messages()[4]], message_index=0)],
        "provider_payload_context_absent",
    )


def test_a_non_canonical_row_keeps_its_own_unjoined_reason() -> None:
    """A gateway-lane row must not be re-labelled with a payload reason."""
    ledger = _ledger([{
        "outcome": "delivered",
        "layer": "l3.contract",
        "event_type": "evidence_delivered",
        "chars_delivered": 40,
        "content_sha256_16": "b" * 16,
    }])
    only = _ledger_only(ledger)
    assert len(only) == 1
    assert only[0]["physical_join_reason"] == "delivery_unjoined"


# ---------------------------------------------------------------------- #
# The trajectory substrate remains authoritative when it CAN see the bytes.
# ---------------------------------------------------------------------- #
def test_the_trajectory_seal_join_still_wins_when_the_bytes_are_visible() -> None:
    """M4. One physical delivery, one entry — never one per substrate."""
    trajectory = _trajectory()
    trajectory[3] = {"role": "tool", "content": OBSERVATION + CAPSULE}
    messages = _payload_messages()
    messages[3]["content"][0]["text"] = OBSERVATION + CAPSULE
    row = _canonical_row(payload_messages=messages)
    row["iteration"] = 1
    ledger = _ledger([row], trajectory)
    assert _payload_entries(ledger) == []
    joined = [e for e in ledger["entries"] if e.get("joined") is True]
    assert len(joined) == 1
    assert joined[0]["source"] == "trajectory"
    assert joined[0]["join_method"] == "seal"
    assert ledger["ledger_rows_joined"] == 1


def test_a_payload_join_proves_a_cap_byte_owner() -> None:
    """The byte-owner gate reads the same authority; a payload-substrate join
    must satisfy it exactly as a trajectory seal join does."""
    import gt_feature_metrics as gfm

    row = _canonical_row()
    row["evidence_lineage"] = [{
        "candidate_id": "ac032ea694307691",
        "fact_class": "localization",
        "cap_owners": ["GT_LOC_RESLOT"],
    }]
    ledger = _ledger([row])
    assert gfm._member_delivery_byte_proven("GT_LOC_RESLOT", [row], ledger) is True
