"""End-to-end orchestrator + telemetry tests for v5."""

from __future__ import annotations

import json
from pathlib import Path

from groundtruth.pretask.brief_v5 import BriefResult, generate_brief

ISSUE_TEXT = """SafeWatchdog._fd is not closed on Postmaster shutdown.

Repro:
```
File "patroni/postmaster.py", line 89, in start
  self.watchdog.activate()
File "patroni/watchdog.py", line 142, in activate
  self._fd.write(b"\\x56")
OSError: Bad file descriptor
```

See `patroni/watchdog.py` and tests/test_watchdog.py.
"""

# Required telemetry record keys per arch_update.md §3.
_REQUIRED_TOP_KEYS = {
    "task_id",
    "timestamp",
    "version",
    "input",
    "module_1_anchors",
    "module_2_traces",
    "module_3_ppr",
    "module_4_recent",
    "module_6_hybrid",
    "module_7_cochange",
    "module_7_contract",
    "module_7_constraints",
    "module_5_render",
    "total_wall_ms",
    "brief_text",
}
_REQUIRED_INPUT_KEYS = {
    "issue_chars",
    "repo_root",
    "graph_db_size_kb",
    "graph_node_count",
    "graph_edge_count",
}
_REQUIRED_M1_KEYS = {
    "wall_ms",
    "symbols_extracted_raw",
    "symbols_after_stopword",
    "symbols_resolved_in_graph",
    "paths_extracted",
    "test_names_extracted",
}
_REQUIRED_M2_KEYS = {
    "wall_ms",
    "raw_frames_found",
    "in_repo_frames",
    "deepest_frame",
}
_REQUIRED_M3_KEYS = {
    "wall_ms",
    "seed_node_count",
    "seed_node_names",
    "iterations_to_convergence",
    "top_10_files",
}
_REQUIRED_M4_KEYS = {
    "wall_ms",
    "git_log_entries",
    "files_with_recent_edits",
    "boosts_applied",
}
_REQUIRED_M5_KEYS = {
    "wall_ms",
    "candidates_pre_filter",
    "candidates_in_brief",
    "rationale_tags",
    "brief_chars",
    "abstained",
}
_REQUIRED_M6_KEYS = {
    "wall_ms",
    "signal_counts",
    "commits_examined",
    "matching_commits",
    "fused_candidates",
    "confidence_counts",
}
_REQUIRED_M7_COCHANGE_KEYS = {
    "wall_ms",
    "enabled",
    "primary_files",
    "commits_examined",
    "commits_with_primary",
    "cluster_files",
    "rejected_files",
    "abstain_reason",
}
_REQUIRED_M7_CONTRACT_KEYS = {
    "wall_ms",
    "enabled",
    "test_files_considered",
    "selected_test_files",
    "contract_lines",
    "issue_calls",
    "extraction_mode",
    "abstain_reason",
}
_REQUIRED_M7_CONSTRAINTS_KEYS = {
    "wall_ms",
    "enabled",
    "constraints",
    "detected_test_layout",
    "scaffold_patterns",
    "negative_space_patterns",
    "hook_warning_fired",
}


def test_generate_brief_returns_xml_block(tiny_graph_db: str, tmp_path: Path) -> None:
    """End-to-end: brief is non-empty and surfaces watchdog file."""
    brief = generate_brief(
        ISSUE_TEXT,
        repo_root=str(tmp_path),
        graph_db=tiny_graph_db,
        task_id="t1",
        log_dir=str(tmp_path / "logs"),
    )
    assert isinstance(brief, str)
    assert "<gt-task-brief>" in brief
    assert "</gt-task-brief>" in brief
    assert "patroni/watchdog.py" in brief


def test_telemetry_record_full_schema(
    tiny_graph_db: str, tmp_path: Path
) -> None:
    """Every spec key in §3 is populated, even when empty."""
    log_dir = tmp_path / "logs"
    result = generate_brief(
        ISSUE_TEXT,
        repo_root=str(tmp_path),
        graph_db=tiny_graph_db,
        task_id="schema_test",
        log_dir=str(log_dir),
        return_telemetry=True,
    )
    assert isinstance(result, BriefResult)

    rec = result.telemetry.as_dict()
    assert _REQUIRED_TOP_KEYS.issubset(rec.keys())
    assert _REQUIRED_INPUT_KEYS.issubset(rec["input"].keys())
    assert _REQUIRED_M1_KEYS.issubset(rec["module_1_anchors"].keys())
    assert _REQUIRED_M2_KEYS.issubset(rec["module_2_traces"].keys())
    assert _REQUIRED_M3_KEYS.issubset(rec["module_3_ppr"].keys())
    assert _REQUIRED_M4_KEYS.issubset(rec["module_4_recent"].keys())
    assert _REQUIRED_M6_KEYS.issubset(rec["module_6_hybrid"].keys())
    assert _REQUIRED_M7_COCHANGE_KEYS.issubset(rec["module_7_cochange"].keys())
    assert _REQUIRED_M7_CONTRACT_KEYS.issubset(rec["module_7_contract"].keys())
    assert _REQUIRED_M7_CONSTRAINTS_KEYS.issubset(
        rec["module_7_constraints"].keys()
    )
    assert _REQUIRED_M5_KEYS.issubset(rec["module_5_render"].keys())
    assert rec["module_6_hybrid"]["fused_candidates"]
    assert rec["module_7_cochange"]["enabled"] is False
    assert rec["module_7_contract"]["extraction_mode"] == "not_implemented"
    assert "*_demo.py" in rec["module_7_constraints"]["scaffold_patterns"]
    assert "test-of-affected-class" in result.brief

    # The brief was rendered, file written.
    assert result.telemetry_path is not None
    written = Path(result.telemetry_path)
    assert written.exists()
    line = written.read_text(encoding="utf-8").strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["task_id"] == "schema_test"
    assert parsed["version"] == "v5.0"
    assert "module_7_cochange" in parsed
    assert parsed["module_7_constraints"]["hook_warning_fired"] is False


def test_abstain_when_no_signal(tmp_path: Path) -> None:
    """Issue with no symbols / no DB → abstain message."""
    log_dir = tmp_path / "logs"
    brief = generate_brief(
        "the test failed and the fix did not work",
        repo_root=str(tmp_path),
        graph_db=None,
        task_id="abstain_t",
        log_dir=str(log_dir),
    )
    assert "could not deterministically localize" in brief


def test_ppr_seeded_by_issue_keywords(
    tiny_graph_db: str, tmp_path: Path
) -> None:
    """Fix #2: PPR seeds are populated from issue-text symbols, not empty.

    Verifies that ``anchors.symbols_resolved_in_graph`` flows into
    ``module_3_ppr.seed_node_names`` — i.e. the personalised PageRank is
    actually personalised by the issue keywords, not run with an empty
    seed set.
    """
    log_dir = tmp_path / "logs"
    result = generate_brief(
        ISSUE_TEXT,
        repo_root=str(tmp_path),
        graph_db=tiny_graph_db,
        task_id="ppr_seed_test",
        log_dir=str(log_dir),
        return_telemetry=True,
    )
    assert isinstance(result, BriefResult)
    rec = result.telemetry.as_dict()
    resolved = set(rec["module_1_anchors"]["symbols_resolved_in_graph"])
    seeds = set(rec["module_3_ppr"]["seed_node_names"])
    assert resolved, "anchors should resolve at least one symbol from the issue"
    assert seeds, "PPR must run with non-empty seeds when anchors resolve"
    assert seeds & resolved, (
        f"PPR seeds ({seeds}) must overlap anchors.symbols_resolved_in_graph ({resolved})"
    )
    assert rec["module_3_ppr"]["seed_node_count"] >= 1
