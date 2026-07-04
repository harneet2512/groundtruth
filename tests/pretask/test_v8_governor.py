from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.pretask.v8_governor import (
    AgentCandidate,
    agent_path_allowed,
    extract_agent_candidates,
    govern,
    pilot_stop_condition,
)


def _rec(
    path: str,
    score: float,
    *,
    entered_via: str = "semantic_seed",
    reach: float = 0.0,
    anchor_prox: float = 0.0,
    path_len: int = 999,
) -> dict:
    return {
        "path": path,
        "score": score,
        "entered_via": entered_via,
        "min_path_length_from_anchor": path_len,
        "components": {
            "reach": reach,
            "anchor_prox": anchor_prox,
            "sem": score,
            "lex": 0.0,
            "hub_pen": 0.0,
            "commit": 0.0,
        },
    }


def test_agent_path_extraction_is_code_first_for_generic_evidence() -> None:
    text = "opened src/core.py docs/guide.md pyproject.toml tests/test_core.py fixtures/data.json"

    paths = [c.path for c in extract_agent_candidates(text, evidence="generic_open")]

    assert "src/core.py" in paths
    assert "tests/test_core.py" in paths
    assert "docs/guide.md" not in paths
    assert "pyproject.toml" not in paths
    assert "fixtures/data.json" not in paths


def test_agent_path_extraction_allows_non_code_only_with_strong_evidence() -> None:
    text = "pytest failed while loading docs/schema.json and src/core.py"

    paths = [c.path for c in extract_agent_candidates(text, evidence="failing_test_output")]

    assert "docs/schema.json" in paths
    assert "src/core.py" in paths
    assert agent_path_allowed("pyproject.toml", "generic_search") is False
    assert agent_path_allowed("pyproject.toml", "command_output") is True


def test_governor_expands_once_with_unused_gt_structural_rescues() -> None:
    gt = [
        _rec("src/a.py", 0.10, reach=0.10, anchor_prox=0.0, path_len=999),
        _rec("src/b.py", 0.09, reach=0.10, anchor_prox=0.0, path_len=999),
        _rec("src/c.py", 0.08, reach=0.10, anchor_prox=0.0, path_len=999),
        _rec("src/rescue1.py", 0.07, entered_via="graph_rescue", reach=0.40, path_len=1),
        _rec("src/reject_semantic.py", 0.055, entered_via="semantic_seed", reach=0.0, path_len=999),
        _rec("src/rescue2.py", 0.06, entered_via="both", reach=0.35, path_len=2),
        _rec("src/rescue3.py", 0.05, entered_via="graph_rescue", reach=0.60, path_len=1),
    ]

    result = govern(gt, [], preferred_max=3, hard_ceiling=7)

    assert result.expanded is True
    assert result.expansion_reason == "low_top_score"
    assert result.expansion_added == ["src/rescue1.py", "src/rescue2.py"]
    assert "src/reject_semantic.py" not in result.expansion_added
    assert len(result.expansion_added) == 2
    assert len(result.active_set) <= 7


def test_governor_uses_graph_ring_when_gt_rescue_slots_remain(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            file_path TEXT NOT NULL
        );
        CREATE TABLE edges (
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            confidence REAL
        );
        INSERT INTO nodes (id, file_path) VALUES
            (1, 'src/top.py'),
            (2, 'src/ring.py'),
            (3, 'src/weak.py');
        INSERT INTO edges (source_id, target_id, confidence) VALUES
            (1, 2, 0.8),
            (1, 3, 0.4);
        """
    )
    conn.close()
    gt = [
        _rec("src/top.py", 0.10, entered_via="both", reach=0.10, anchor_prox=0.333, path_len=0),
        _rec("src/other.py", 0.09, reach=0.10, path_len=999),
    ]

    result = govern(gt, [], graph_db=str(db), preferred_max=2, hard_ceiling=4)

    assert result.expanded is True
    assert result.expansion_added == ["src/ring.py"]


def test_trace_probe_is_fallback_only_and_uses_strong_trace_text() -> None:
    gt = [
        _rec("src/a.py", 0.10),
        _rec("src/b.py", 0.09),
        _rec("src/c.py", 0.08),
    ]
    trace = "FAILED loading config tests/fixtures/case.json\nFile \"src/failure.py\", line 12"

    result = govern(gt, [], early_trace_text=trace, preferred_max=3, hard_ceiling=5)

    assert result.expanded is True
    assert result.expansion_added == ["tests/fixtures/case.json", "src/failure.py"]


def test_pilot_stop_condition_corrected_dumb_union_rule() -> None:
    rows = [
        {
            "gold_files": ["src/gold.py"],
            "governor_files": ["src/gold.py", "src/a.py", "src/b.py"],
            "dumb_union_files": ["src/gold.py", "src/a.py"],
        },
        {
            "gold_files": ["src/missed.py"],
            "governor_files": ["src/a.py"],
            "dumb_union_files": ["src/missed.py", "src/a.py"],
        },
    ]
    assert pilot_stop_condition(rows)["PASS"] is True

    too_many_worse = rows * 2
    assert pilot_stop_condition(too_many_worse)["PASS"] is False

    equal_no_count_advantage = [
        {
            "gold_files": ["src/gold.py"],
            "governor_files": ["src/gold.py", "src/a.py"],
            "dumb_union_files": ["src/gold.py", "src/a.py"],
        }
        for _ in range(10)
    ]
    assert pilot_stop_condition(equal_no_count_advantage)["PASS"] is False


def test_high_support_zero_overlap_triggers_expansion() -> None:
    gt = [
        _rec("src/gt1.py", 1.0, entered_via="both", reach=0.6, anchor_prox=0.333, path_len=1),
        _rec("src/gt2.py", 0.9, entered_via="graph_rescue", reach=0.5, path_len=1),
        _rec("src/gt3.py", 0.8, entered_via="graph_rescue", reach=0.5, path_len=1),
        _rec("src/gt4.py", 0.7, entered_via="graph_rescue", reach=0.5, path_len=1),
    ]
    agent = [
        AgentCandidate("src/agent1.py", 1.0, "generic_open"),
        AgentCandidate("src/agent2.py", 0.95, "generic_open"),
        AgentCandidate("src/agent3.py", 0.90, "generic_open"),
    ]

    result = govern(gt, agent, preferred_max=3, hard_ceiling=5)

    assert result.expanded is True
    assert result.expansion_reason in {"top3_close", "high_support_zero_overlap"}
