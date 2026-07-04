"""Tests for telemetry schema validation."""
from __future__ import annotations

import pytest
from groundtruth.telemetry.schemas import (
    GTLayerEvent, GTAgentReactionEvent, GTBeliefEvent,
    EvidenceItem, EvidenceKind, get_iteration_band,
)


class TestGetIterationBand:
    def test_early(self):
        assert get_iteration_band(0, 100) == "early_0_25"
        assert get_iteration_band(10, 100) == "early_0_25"
        assert get_iteration_band(24, 100) == "early_0_25"

    def test_mid(self):
        assert get_iteration_band(25, 100) == "mid_25_60"
        assert get_iteration_band(59, 100) == "mid_25_60"

    def test_late(self):
        assert get_iteration_band(60, 100) == "late_60_85"
        assert get_iteration_band(84, 100) == "late_60_85"

    def test_final(self):
        assert get_iteration_band(85, 100) == "final_85_100"
        assert get_iteration_band(99, 100) == "final_85_100"

    def test_zero_max(self):
        assert get_iteration_band(0, 0) == "early_0_25"


class TestEvidenceItem:
    def test_valid_kind(self):
        item = EvidenceItem(kind="l3_caller_code", text="foo()")
        assert item.token_estimate == 1

    def test_invalid_kind_raises(self):
        with pytest.raises(ValueError, match="Invalid evidence kind"):
            EvidenceItem(kind="invalid_kind")

    def test_auto_item_id(self):
        item = EvidenceItem(kind="l3_signature")
        assert len(item.item_id) == 16

    def test_to_dict(self):
        item = EvidenceItem(kind="l3_caller_code", file_path="src/foo.py", text="bar()")
        d = item.to_dict()
        assert d["kind"] == "l3_caller_code"
        assert d["file_path"] == "src/foo.py"


class TestEvidenceKind:
    def test_all_kinds_exist(self):
        assert len(EvidenceKind) == 20

    def test_l3_kinds(self):
        assert EvidenceKind.L3_CALLER_CODE.value == "l3_caller_code"
        assert EvidenceKind.L3_SIGNATURE.value == "l3_signature"


class TestGTLayerEvent:
    def test_required_fields(self):
        event = GTLayerEvent(layer="L3", event_type="evidence", eligible=True, emitted=True, suppressed=False)
        assert event.schema_version == "1.0.0"
        assert len(event.event_id) == 16
        assert event.timestamp_ms > 0

    def test_invalid_layer_raises(self):
        with pytest.raises(ValueError, match="Invalid layer"):
            GTLayerEvent(layer="INVALID", event_type="x", eligible=True, emitted=True, suppressed=False)

    def test_auto_iteration_band(self):
        event = GTLayerEvent(layer="L3", event_type="x", eligible=True, emitted=True, suppressed=False, iter=70, max_iter=100)
        assert event.iteration_band == "late_60_85"

    def test_auto_rendered_chars(self):
        event = GTLayerEvent(layer="L1", event_type="x", eligible=True, emitted=True, suppressed=False, rendered_text="hello world")
        assert event.rendered_chars == 11
        assert event.rendered_tokens_estimate == 2

    def test_to_dict_includes_required(self):
        event = GTLayerEvent(layer="L5", event_type="detection", eligible=True, emitted=True, suppressed=False)
        d = event.to_dict()
        assert "schema_version" in d
        assert "event_id" in d
        assert "layer" in d


class TestGTAgentReactionEvent:
    def test_valid_follow_type(self):
        event = GTAgentReactionEvent(gt_event_id="abc", gt_layer="L3", gt_iter=5, follow_type="FOLLOWED_EXACT")
        assert event.schema_version == "1.0.0"

    def test_invalid_follow_type_raises(self):
        with pytest.raises(ValueError, match="Invalid follow_type"):
            GTAgentReactionEvent(gt_event_id="abc", gt_layer="L3", gt_iter=5, follow_type="INVALID")


class TestGTBeliefEvent:
    def test_valid_status(self):
        event = GTBeliefEvent(file_path="src/foo.py", new_status="candidate", reason="L1 hit", source_event_id="abc")
        assert event.schema_version == "1.0.0"

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError, match="Invalid belief status"):
            GTBeliefEvent(file_path="x", new_status="INVALID", reason="x", source_event_id="x")
