"""Task-start brief localization has its own honest evidence boundary."""

from __future__ import annotations

import hashlib
import json

from artifact_deepswe import gt_headless_runner as runner
from groundtruth.runtime.fact_registry import (
    EVENT_SEARCH_RESULT,
    EVENT_TASK_START,
    producer_matches,
    registration_for,
    required_event,
)


def _write_brief_result(tmp_path, brief: str, *, label: str) -> dict[str, str]:
    block = brief
    digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
    result = {
        "schema": "gt.brief_result.v1",
        "brief_text": brief,
        "metrics": {
            "block_receipts": [{
                "block_id": f"{label}:candidate-1",
                "candidate_id": "localization:src/example.py",
                "char_span": [0, len(block)],
                "content_hash": digest,
                "fact_class": "localization",
                "label": label,
            }],
        },
    }
    (tmp_path / "brief.txt").write_text(brief, encoding="utf-8")
    (tmp_path / "brief_result.json").write_text(
        json.dumps(result), encoding="utf-8")
    return {"GT_BRIEF_FILE": str(tmp_path / "brief.txt")}


def test_brief_and_reactive_localization_have_distinct_boundaries() -> None:
    assert registration_for("brief_localization").fact_class == "localization"
    assert required_event("brief_localization") == EVENT_TASK_START
    assert producer_matches("brief_localization", "v1r_brief")

    assert registration_for("localization").fact_class == "localization"
    assert required_event("localization") == EVENT_SEARCH_RESULT


def test_file_entry_lineage_uses_task_start_evidence_type(tmp_path) -> None:
    brief = "1. src/example.py (Example)"
    extra = runner._brief_delivery_extra(
        _write_brief_result(tmp_path, brief, label="file-entry-1"), brief)

    block = extra["block_lineage"][0]
    assert block["declared_fact_class"] == "localization"
    assert block["lineage"]["evidence_type"] == "brief_localization"
    assert block["lineage"]["actual_event"] == EVENT_TASK_START
    assert block["lineage"]["producer_registration_match"] is True


def test_localization_header_uses_same_task_start_contract(tmp_path) -> None:
    brief = '<gt-localization confidence="high">x</gt-localization>'
    extra = runner._brief_delivery_extra(
        _write_brief_result(tmp_path, brief, label="localization-header"), brief)

    assert (
        extra["block_lineage"][0]["lineage"]["evidence_type"]
        == "brief_localization"
    )
