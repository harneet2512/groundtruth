"""Tests for evidence helpers."""
from __future__ import annotations

import pytest
from groundtruth.telemetry.evidence import make_evidence_item, truncate_evidence_by_priority


class TestMakeEvidenceItem:
    def test_valid_item(self):
        item = make_evidence_item(kind="l3_caller_code", file_path="src/foo.py", text="bar(x, y)")
        assert item["kind"] == "l3_caller_code"
        assert item["file_path"] == "src/foo.py"
        assert item["token_estimate"] >= 1

    def test_invalid_kind(self):
        with pytest.raises(ValueError):
            make_evidence_item(kind="bogus")


class TestTruncateEvidence:
    def test_under_cap(self):
        items = [make_evidence_item(kind="l3_caller_code", text="short")]
        kept, reason = truncate_evidence_by_priority(items, 300)
        assert len(kept) == 1
        assert reason is None

    def test_over_cap_cuts_low_priority(self):
        items = [
            make_evidence_item(kind="l3_caller_code", text="a" * 800),
            make_evidence_item(kind="l3_sibling_pattern", text="b" * 800),
        ]
        kept, reason = truncate_evidence_by_priority(items, 300)
        assert len(kept) == 1
        assert kept[0]["kind"] == "l3_caller_code"
        assert reason is not None

    def test_empty_list(self):
        kept, reason = truncate_evidence_by_priority([], 300)
        assert kept == []
        assert reason is None
