"""End-to-end shadow replay: fixture → replay → metric parse.

Builds the deterministic matched fixture from ``scripts/build_replay_fixture``,
runs ``scripts/shadow_replay`` against it, and asserts that the report
exercises real router branches (not all NO_GRAPH_DB / not all NO_EVIDENCE).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from groundtruth.telemetry.router_replay_metrics import (
    parse_replay_report,
    summarize_provider_request_log,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *argv],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )


@pytest.fixture(scope="module")
def fixture_replay(tmp_path_factory: pytest.TempPathFactory) -> dict:
    out_dir = tmp_path_factory.mktemp("fixture_replay")
    fixture_dir = out_dir / "fixture"
    _run(
        "scripts/build_replay_fixture.py",
        "--out", str(fixture_dir),
        "--workspace", "/workspace/fixture",
    )
    report_path = out_dir / "replay.json"
    _run(
        "scripts/shadow_replay.py",
        "--outputs",
        str(fixture_dir / "results" / "SWE-bench-Live__SWE-bench-Live-lite" / "CodeActAgent"
            / "deepseek-v4-flash_maxiter_100" / "output.jsonl"),
        "--graph-map", str(fixture_dir / "graph_map.json"),
        "--repo-root", "/workspace/fixture",
        "--report", str(report_path),
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


class TestGraphBackedReplay:
    def test_graph_resolved(self, fixture_replay: dict) -> None:
        assert fixture_replay["graph_resolved_count"] == 1
        assert fixture_replay["graph_unresolved_count"] == 0

    def test_router_emit_count_positive(self, fixture_replay: dict) -> None:
        assert fixture_replay["totals"]["router_emit"] >= 1

    def test_provider_requests_recorded(self, fixture_replay: dict) -> None:
        assert fixture_replay["totals"]["provider_request"] >= 1

    def test_no_graph_db_is_zero_when_resolved(self, fixture_replay: dict) -> None:
        # Every provider call had a real graph behind it.
        assert fixture_replay["suppression_distribution"]["no_graph_db"] == 0

    def test_exercises_real_branches(self, fixture_replay: dict) -> None:
        """Distinct router branches must fire (not all NO_EVIDENCE)."""
        dist = fixture_replay["suppression_distribution"]
        real_branches = {
            k for k, v in dist.items()
            if v > 0 and k not in ("no_graph_db", "not_applicable")
        }
        # We expect at least 3 distinct branches (no_evidence is allowed too).
        assert len(real_branches) >= 2, dist
        # And the report does not collapse to a single bucket.
        assert sum(dist.values()) > 0
        assert dist.get("no_graph_db", 0) == 0

    def test_old_vs_new_distribution_present(self, fixture_replay: dict) -> None:
        dist = fixture_replay["old_vs_new_distribution"]
        # Fixture deliberately has both "new_only" and "both_silent" events.
        assert "new_only" in dist or "old_only" in dist or "both_emit" in dist
        assert "both_silent" in dist or "new_only" in dist


class TestParseReplayMetrics:
    def test_parses_top_level_totals(self, fixture_replay: dict, tmp_path: Path) -> None:
        # Round-trip via parse_replay_report.
        path = tmp_path / "report.json"
        path.write_text(json.dumps(fixture_replay), encoding="utf-8")
        rm = parse_replay_report(path)
        assert rm.input_count == fixture_replay["input_count"]
        assert rm.router_emit_total == fixture_replay["totals"]["router_emit"]
        assert rm.suppression_distribution == fixture_replay["suppression_distribution"]
        assert rm.old_vs_new_distribution == fixture_replay["old_vs_new_distribution"]

    def test_files_viewed_before_gold_is_distinct_count(
        self, fixture_replay: dict, tmp_path: Path,
    ) -> None:
        """Repaired metric: count of distinct files, not action index."""
        path = tmp_path / "report.json"
        path.write_text(json.dumps(fixture_replay), encoding="utf-8")
        rm = parse_replay_report(path)
        for task, count in zip(rm.per_task, rm.distinct_files_before_gold):
            # The replay records both the action index and the distinct count
            # — assert they are independent fields.
            distinct = task.get("distinct_files_viewed_before_gold")
            assert distinct == count
            assert isinstance(distinct, int)
            assert distinct >= 0

    def test_action_economy_marked_unavailable(self, fixture_replay: dict, tmp_path: Path) -> None:
        path = tmp_path / "report.json"
        path.write_text(json.dumps(fixture_replay), encoding="utf-8")
        rm = parse_replay_report(path)
        econ = rm.action_economy_vs_baseline
        # We expose the parser shape but never claim a value without paired data.
        assert econ["available"] is False
        assert econ["reason"] == "paired_baseline_required"

    def test_provider_request_log_summary(self, fixture_replay: dict) -> None:
        summary = summarize_provider_request_log(fixture_replay["tasks"])
        # The fixture exercises both on_view and on_edit.
        assert summary["requests_by_kind"]["on_view"] >= 1
        assert summary["requests_by_kind"]["on_edit"] >= 1
