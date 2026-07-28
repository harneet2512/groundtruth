"""WIDE-08: issue-spec echoes cannot mint extracted-obligation proof."""

import hashlib
import json

from groundtruth.pretask import v1r_brief as v1r


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
