"""Tests for Phase 5: evidence_precision, evidence_recall, agent_uptake."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from groundtruth.metrics.evidence_scorer import (
    EvidenceScore,
    InjectionRecord,
    compute_precision,
    compute_recall,
    compute_uptake,
    parse_gold_patch,
    parse_injections,
    score_run,
)


SAMPLE_GOLD_PATCH = """\
diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -10,7 +10,7 @@ def validate_token(token: str) -> bool:
-    return token.startswith("valid")
+    return token.startswith("valid") and len(token) > 5
diff --git a/src/models.py b/src/models.py
--- a/src/models.py
+++ b/src/models.py
@@ -25,3 +25,5 @@ class User:
+    def is_active(self) -> bool:
+        return self.status == "active"
"""


class TestParseGoldPatch:
    def test_extracts_files(self):
        files = parse_gold_patch(SAMPLE_GOLD_PATCH)
        assert len(files) == 2
        assert files[0]["file"] == "src/auth.py"
        assert files[1]["file"] == "src/models.py"

    def test_extracts_functions(self):
        files = parse_gold_patch(SAMPLE_GOLD_PATCH)
        assert "validate_token" in files[0]["functions"]

    def test_empty_patch(self):
        assert parse_gold_patch("") == []


class TestParseInjections:
    def test_parses_jsonl(self, tmp_path: Path):
        log = tmp_path / "gt_log.jsonl"
        log.write_text(
            json.dumps({"layer": "L3", "file_path": "src/auth.py",
                        "symbol": "validate_token",
                        "rendered_text": "[SIGNATURE] def validate_token(token: str) -> bool"})
            + "\n"
            + json.dumps({"layer": "L3b", "file_path": "src/models.py",
                          "symbol": "User",
                          "rendered_text": "Callers: routes.py (3x)"})
            + "\n",
            encoding="utf-8",
        )
        records = parse_injections(str(log))
        assert len(records) == 2
        assert records[0].layer == "L3"
        assert records[0].target_function == "validate_token"
        assert "[SIGNATURE]" in records[0].markers

    def test_missing_file(self):
        assert parse_injections("/nonexistent/path.jsonl") == []

    def test_malformed_lines(self, tmp_path: Path):
        log = tmp_path / "bad.jsonl"
        log.write_text("not json\n{}\n", encoding="utf-8")
        records = parse_injections(str(log))
        assert len(records) == 1


class TestComputePrecision:
    def test_correct_injection(self):
        injections = [
            InjectionRecord(layer="L3", file_path="src/auth.py",
                            content="[SIGNATURE] validate_token", markers=["[SIGNATURE]"],
                            char_count=30, target_function="validate_token"),
        ]
        gold = [{"file": "src/auth.py", "functions": ["validate_token"]}]
        correct, total, _ = compute_precision(injections, gold)
        assert correct == 1
        assert total == 1

    def test_wrong_file_injection(self):
        injections = [
            InjectionRecord(layer="L3", file_path="src/utils.py",
                            content="[SIGNATURE] helper", markers=["[SIGNATURE]"],
                            char_count=20, target_function="helper"),
        ]
        gold = [{"file": "src/auth.py", "functions": ["validate_token"]}]
        correct, total, _ = compute_precision(injections, gold)
        assert correct == 0
        assert total == 1

    def test_empty_content_skipped(self):
        injections = [
            InjectionRecord(layer="L3", file_path="src/auth.py",
                            content="", markers=[], char_count=0),
        ]
        gold = [{"file": "src/auth.py", "functions": []}]
        correct, total, _ = compute_precision(injections, gold)
        assert total == 0

    def test_mixed_precision(self):
        injections = [
            InjectionRecord(layer="L3", file_path="src/auth.py",
                            content="sig", markers=[], char_count=3,
                            target_function="validate_token"),
            InjectionRecord(layer="L3", file_path="src/noise.py",
                            content="noise", markers=[], char_count=5,
                            target_function="unrelated"),
        ]
        gold = [{"file": "src/auth.py", "functions": ["validate_token"]}]
        correct, total, _ = compute_precision(injections, gold)
        assert correct == 1
        assert total == 2


class TestComputeRecall:
    def test_full_recall(self):
        injections = [
            InjectionRecord(layer="L3", file_path="src/auth.py",
                            content="evidence", markers=[], char_count=8,
                            target_function="validate_token"),
        ]
        gold = [{"file": "src/auth.py", "functions": ["validate_token"]}]
        delivered, needed = compute_recall(injections, gold)
        assert delivered == 2  # file + function
        assert needed == 2

    def test_partial_recall(self):
        injections = [
            InjectionRecord(layer="L3", file_path="src/auth.py",
                            content="evidence", markers=[], char_count=8,
                            target_function="validate_token"),
        ]
        gold = [
            {"file": "src/auth.py", "functions": ["validate_token"]},
            {"file": "src/models.py", "functions": ["is_active"]},
        ]
        delivered, needed = compute_recall(injections, gold)
        assert needed == 4  # 2 files + 2 functions
        assert delivered == 2  # only auth.py + validate_token

    def test_zero_recall(self):
        injections = []
        gold = [{"file": "src/auth.py", "functions": ["validate_token"]}]
        delivered, needed = compute_recall(injections, gold)
        assert delivered == 0
        assert needed == 2


class TestComputeUptake:
    def test_agent_uses_evidence(self):
        injections = [
            InjectionRecord(layer="L3", file_path="src/auth.py",
                            content="validate_token sig", markers=[], char_count=20,
                            target_function="validate_token"),
        ]
        actions = [
            {"action_type": "observe", "text": ""},
            {"action_type": "FileEditAction", "text": "editing validate_token in auth.py"},
        ]
        hits, opps = compute_uptake(injections, actions)
        assert hits == 1
        assert opps == 1

    def test_agent_ignores_evidence(self):
        injections = [
            InjectionRecord(layer="L3", file_path="src/auth.py",
                            content="validate_token sig", markers=[], char_count=20,
                            target_function="validate_token"),
        ]
        actions = [
            {"action_type": "observe", "text": ""},
            {"action_type": "CmdRunAction", "text": "grep -r 'something_else'"},
        ]
        hits, opps = compute_uptake(injections, actions)
        assert hits == 0
        assert opps == 1

    def test_no_edit_actions(self):
        injections = [
            InjectionRecord(layer="L3", file_path="src/auth.py",
                            content="sig", markers=[], char_count=3),
        ]
        actions = [{"action_type": "observe", "text": ""}]
        hits, opps = compute_uptake(injections, actions)
        assert hits == 0
        assert opps == 1


class TestScoreRun:
    def test_end_to_end(self, tmp_path: Path):
        gold_path = tmp_path / "gold.diff"
        gold_path.write_text(SAMPLE_GOLD_PATCH, encoding="utf-8")

        gt_log = tmp_path / "gt_log.jsonl"
        gt_log.write_text(
            json.dumps({"layer": "L3", "file_path": "src/auth.py",
                        "symbol": "validate_token",
                        "rendered_text": "[SIGNATURE] def validate_token(token: str) -> bool"})
            + "\n",
            encoding="utf-8",
        )

        score = score_run(str(gt_log), str(gold_path))
        assert score.precision == 1.0
        assert score.recall > 0.0
        assert score.total_injections == 1
        assert score.correct_injections == 1

    def test_summary_format(self):
        s = EvidenceScore(precision=0.75, recall=0.5, uptake=0.33,
                          total_injections=4, correct_injections=3,
                          gold_contexts_needed=6, gold_contexts_delivered=3,
                          uptake_opportunities=3, uptake_hits=1)
        text = s.summary()
        assert "precision=0.75" in text
        assert "recall=0.50" in text
        assert "uptake=0.33" in text
