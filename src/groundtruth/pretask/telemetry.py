"""Telemetry schema and writer for the v5 pre-task brief pipeline.

This module owns the on-disk JSONL contract that downstream verifying
agents will consume. The schema lives in §3 of the v5 architecture doc.

Important rule: every key in the schema MUST be populated, even when the
producing module produced nothing. Empty results are logged as ``[]`` /
``null`` so absence of data is observable, not silent.

Side effects: writes one JSON object per call to a per-task ``.jsonl``
file under ``$GT_LOG_DIR`` (default ``/tmp/gt_logs``). The writer is best
effort — IOError never propagates. Module timing is logged separately
from the brief-render time so the JSONL write does not contaminate
``total_wall_ms``.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "v5.0"


@dataclass
class TelemetryRecord:
    """Container for one task's full telemetry record.

    Each ``module_*`` field is a free-form dict so module authors can
    include language-specific extras without breaking the writer; the
    spec keys (wall_ms, etc.) are still required.
    """

    task_id: str
    timestamp: str
    version: str = VERSION
    input: dict[str, Any] = field(default_factory=dict)
    module_1_anchors: dict[str, Any] = field(default_factory=dict)
    module_2_traces: dict[str, Any] = field(default_factory=dict)
    module_3_ppr: dict[str, Any] = field(default_factory=dict)
    module_4_recent: dict[str, Any] = field(default_factory=dict)
    module_6_hybrid: dict[str, Any] = field(default_factory=dict)
    module_7_cochange: dict[str, Any] = field(default_factory=dict)
    module_7_contract: dict[str, Any] = field(default_factory=dict)
    module_7_constraints: dict[str, Any] = field(default_factory=dict)
    gt_plan: dict[str, Any] = field(default_factory=dict)
    module_5_render: dict[str, Any] = field(default_factory=dict)
    total_wall_ms: int = 0
    brief_text: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Convert to a JSON-safe dict in the canonical key order."""
        return {
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "version": self.version,
            "input": self.input,
            "module_1_anchors": self.module_1_anchors,
            "module_2_traces": self.module_2_traces,
            "module_3_ppr": self.module_3_ppr,
            "module_4_recent": self.module_4_recent,
            "module_6_hybrid": self.module_6_hybrid,
            "module_7_cochange": self.module_7_cochange,
            "module_7_contract": self.module_7_contract,
            "module_7_constraints": self.module_7_constraints,
            "gt_plan": self.gt_plan,
            "module_5_render": self.module_5_render,
            "total_wall_ms": self.total_wall_ms,
            "brief_text": self.brief_text,
        }


# ----------------------------------------------------------------- defaults
def empty_anchors_block() -> dict[str, Any]:
    """Required keys for module_1_anchors, all defaulted."""
    return {
        "wall_ms": 0,
        "symbols_extracted_raw": [],
        "symbols_after_stopword": [],
        "symbols_resolved_in_graph": [],
        "paths_extracted": [],
        "test_names_extracted": [],
    }


def empty_traces_block() -> dict[str, Any]:
    """Required keys for module_2_traces, all defaulted."""
    return {
        "wall_ms": 0,
        "raw_frames_found": 0,
        "in_repo_frames": [],
        "deepest_frame": None,
    }


def empty_ppr_block() -> dict[str, Any]:
    """Required keys for module_3_ppr, all defaulted."""
    return {
        "wall_ms": 0,
        "seed_node_count": 0,
        "seed_node_names": [],
        "iterations_to_convergence": 0,
        "top_10_files": [],
    }


def empty_recent_block() -> dict[str, Any]:
    """Required keys for module_4_recent, all defaulted."""
    return {
        "wall_ms": 0,
        "git_log_entries": 0,
        "files_with_recent_edits": 0,
        "boosts_applied": [],
    }


def empty_render_block() -> dict[str, Any]:
    """Required keys for module_5_render, all defaulted."""
    return {
        "wall_ms": 0,
        "candidates_pre_filter": 0,
        "candidates_in_brief": 0,
        "rationale_tags": [],
        "brief_chars": 0,
        "abstained": False,
    }


def empty_hybrid_block() -> dict[str, Any]:
    """Required keys for the deterministic hybrid-fusion stage."""
    return {
        "wall_ms": 0,
        "signal_counts": {},
        "commits_examined": 0,
        "matching_commits": 0,
        "fused_candidates": [],
        "confidence_counts": {},
    }


def empty_v7_cochange_block() -> dict[str, Any]:
    """Required keys for v7 git-history change-cluster telemetry."""
    return {
        "wall_ms": 0,
        "enabled": False,
        "primary_files": [],
        "commits_examined": 0,
        "commits_with_primary": 0,
        "cluster_files": [],
        "rejected_files": [],
        "abstain_reason": "not_implemented",
    }


def empty_v7_contract_block() -> dict[str, Any]:
    """Required keys for v7 test/issue contract telemetry."""
    return {
        "wall_ms": 0,
        "enabled": False,
        "test_files_considered": [],
        "selected_test_files": [],
        "contract_lines": [],
        "issue_calls": [],
        "extraction_mode": "not_implemented",
        "abstain_reason": "not_implemented",
    }


def empty_v7_constraints_block() -> dict[str, Any]:
    """Required keys for v7 edit-constraint telemetry."""
    return {
        "wall_ms": 0,
        "enabled": False,
        "constraints": [],
        "detected_test_layout": [],
        "scaffold_patterns": [
            "*_test.py",
            "*_demo.py",
            "*_verification.py",
            "final_*.py",
            "comprehensive_*.py",
        ],
        "negative_space_patterns": ["vendor/", "node_modules/", "*.lock"],
        "hook_warning_fired": False,
    }


def empty_input_block(
    issue_text: str = "",
    repo_root: str = "",
    graph_db: str = "",
) -> dict[str, Any]:
    """Required keys for input, with sane defaults."""
    return {
        "issue_chars": len(issue_text),
        "repo_root": repo_root,
        "graph_db_size_kb": 0,
        "graph_node_count": 0,
        "graph_edge_count": 0,
    }


# ----------------------------------------------------------------- writer
def _log_dir() -> Path:
    """Resolve the directory for telemetry JSONL files."""
    env = os.environ.get("GT_LOG_DIR")
    if env:
        return Path(env)
    return Path("/tmp/gt_logs")


def write_record(
    record: TelemetryRecord,
    log_dir: str | None = None,
) -> str | None:
    """Persist ``record`` as one line to ``<log_dir>/<task>_v5_brief.jsonl``.

    Args:
        record: The fully-populated TelemetryRecord. Empty modules MUST
            already be set (use the ``empty_*_block`` helpers).
        log_dir: Optional override; otherwise resolved from ``$GT_LOG_DIR``.

    Returns:
        The path written on success, or ``None`` on any IO/OS error
        (writer is best effort and never raises).
    """
    target_dir = Path(log_dir) if log_dir else _log_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    safe_id = record.task_id.replace("/", "_").replace("\\", "_") or "unknown"
    version_slug = "v7" if str(record.version).startswith("v7") else "v5"
    target = target_dir / f"{safe_id}_{version_slug}_brief.jsonl"
    try:
        line = json.dumps(record.as_dict(), default=str)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return str(target)
    except (OSError, TypeError, ValueError):
        return None


def append_summary(
    log_dir: str | None,
    summary: dict[str, Any],
) -> str | None:
    """Append one aggregate summary line to ``v5_summary.jsonl``."""
    target_dir = Path(log_dir) if log_dir else _log_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    target = target_dir / "v5_summary.jsonl"
    try:
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, default=str) + "\n")
        return str(target)
    except (OSError, TypeError, ValueError):
        return None


def utc_timestamp() -> str:
    """ISO-8601 UTC timestamp with second precision (no fractional part)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
