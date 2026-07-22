"""WIDE-08: issue-spec echoes cannot mint extracted-obligation proof."""

import hashlib
import json

from groundtruth.pretask import v1r_brief as v1r
from artifact_deepswe import gt_headless_runner as runner


def _artifact(tmp_path, issue: str) -> None:
    (tmp_path / "gt_obligations_v2.json").write_text(json.dumps({
        "issue_sha256": hashlib.sha256(issue.encode()).hexdigest(),
        "clauses": [{"verbatim_text": "must preserve the caller mapping"}],
    }), encoding="utf-8")


def test_expected_behavior_echo_cannot_mint_obligations_record(tmp_path, monkeypatch):
    issue = "Preserve the caller mapping."
    brief = "Expected behavior: preserve the caller mapping"
    digest = hashlib.sha256(brief.encode()).hexdigest()
    receipt = {
        "fact_class": "obligations", "label": "expected-behavior",
        "candidate_id": "brief:block:expected-behavior",
        "char_span": [0, len(brief)], "content_hash": digest,
    }
    _artifact(tmp_path, issue)
    monkeypatch.setattr(v1r, "_anchors_path", lambda: str(tmp_path / "anchors.json"))
    assert v1r._build_obligations_record(brief, [receipt], issue) == {}


def test_canonical_obligations_block_remains_bound(tmp_path, monkeypatch):
    issue = "Preserve the caller mapping."
    brief = "<gt-obligations>\n- preserve the caller mapping\n</gt-obligations>"
    digest = hashlib.sha256(brief.encode()).hexdigest()
    receipt = {
        "fact_class": "obligations", "label": "obligations",
        "candidate_id": "brief:block:obligations",
        "char_span": [0, len(brief)], "content_hash": digest,
    }
    _artifact(tmp_path, issue)
    monkeypatch.setattr(v1r, "_anchors_path", lambda: str(tmp_path / "anchors.json"))
    record = v1r._build_obligations_record(brief, [receipt], issue)
    assert record["schema"] == "gt.obligations_record.v1"
    assert record["candidate_id"] == receipt["candidate_id"]


def test_runner_does_not_register_expected_behavior_as_obligations(tmp_path):
    brief = "Expected behavior: preserve the caller mapping"
    digest = hashlib.sha256(brief.encode()).hexdigest()
    result = {
        "schema": "gt.brief_result.v1", "brief_text": brief,
        "metrics": {"block_receipts": [{
            "block_id": "expected-behavior",
            "candidate_id": "brief:block:expected-behavior",
            "char_span": [0, len(brief)], "content_hash": digest,
            "fact_class": "obligations", "label": "expected-behavior",
        }]},
    }
    brief_path = tmp_path / "brief.txt"
    brief_path.write_text(brief, encoding="utf-8")
    (tmp_path / "brief_result.json").write_text(json.dumps(result), encoding="utf-8")
    extra = runner._brief_delivery_extra({"GT_BRIEF_FILE": str(brief_path)}, brief)
    assert extra["block_lineage"][0]["lineage_status"] == "UNREGISTERED_BLOCK_LABEL"
