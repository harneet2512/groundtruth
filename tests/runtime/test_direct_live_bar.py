"""DIRECT LIVE bar + campaign feature-live honesty channel."""
from __future__ import annotations

import json
from pathlib import Path

from groundtruth.runtime.campaign_feature_live import (
    append_feature_live,
    mirror_ledger_row_to_feature_live,
    stage_from_ledger_row,
)
from groundtruth.runtime.direct_live import (
    DIRECT_IDS,
    FALSE_LIVE_CLAIM_KINDS,
    assert_direct_inventory_closed,
    classify_direct_live,
    is_sealed_delivery,
)


def test_direct_inventory_closed() -> None:
    assert_direct_inventory_closed()
    assert len(DIRECT_IDS) == 17


def test_sealed_delivery_requires_chars_and_seal() -> None:
    assert not is_sealed_delivery({"outcome": "delivered", "chars_delivered": 10})
    assert not is_sealed_delivery({
        "outcome": "delivered", "chars_delivered": 0,
        "content_sha256_16": "abcd1234abcd1234",
    })
    assert is_sealed_delivery({
        "outcome": "delivered",
        "chars_delivered": 12,
        "content_sha256_16": "abcd1234abcd1234",
    })


def test_classify_forbids_profile_on_as_live() -> None:
    assert classify_direct_live(
        opportunity=True,
        sealed_delivery=False,
        false_live_claim="profile_member_on",
    ) == "BROKEN"
    assert "profile_member_on" in FALSE_LIVE_CLAIM_KINDS
    assert classify_direct_live(
        opportunity=True, sealed_delivery=True) == "DELIVERED"
    assert classify_direct_live(
        opportunity=False, sealed_delivery=False) == "NOT_ELIGIBLE"
    assert classify_direct_live(
        opportunity=True, sealed_delivery=False, explicit_hold=True) == "HOLD"


def test_feature_live_rejects_on_as_stage(tmp_path: Path) -> None:
    sink = tmp_path / "gt_campaign_feature_live.jsonl"
    append_feature_live(
        feature_id="GT_EDIT_CHECK",
        stage="ON",  # forbidden — must no-op
        path=str(sink),
    )
    assert not sink.exists()
    append_feature_live(
        feature_id="covering_red",
        stage="HOLD",
        reason="covering_empty_sub_fact_floor",
        path=str(sink),
    )
    rows = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["stage"] == "HOLD"
    assert rows[0]["feature_id"] == "covering_red"


def test_mirror_sealed_delivery_to_feature_live(tmp_path: Path, monkeypatch) -> None:
    sink = tmp_path / "feature_live.jsonl"
    monkeypatch.setenv("GT_CAMPAIGN_FEATURE_LIVE", str(sink))
    mirror_ledger_row_to_feature_live({
        "outcome": "delivered",
        "chars_delivered": 40,
        "content_sha256_16": "0123456789abcdef",
        "fact_class": "obligations",
        "reason": "",
        "layer": "obligation.resurface",
        "iteration": 3,
    })
    rows = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["stage"] == "DELIVERED"
    assert rows[0]["feature_id"] == "obligations"


def test_covering_empty_maps_to_hold() -> None:
    assert stage_from_ledger_row({
        "outcome": "suppressed_hidden_only",
        "reason": "covering_empty_sub_fact_floor",
        "chars_delivered": 0,
    }) == "HOLD"
