from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from groundtruth.pretask.v8_2_scheduler import parse_trace_artifact, schedule_v82


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
        "components": {"reach": reach, "anchor_prox": anchor_prox},
    }


def test_trace_parser_stops_after_first_material_edit_and_tiers_paths(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    task_dir = root / "bug-1"
    task_dir.mkdir(parents=True)
    (task_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "trajectory": [
                    {"action": "read src/a.py"},
                    {"action": "rg Widget src/a.py"},
                    {"tool": "pytest", "observation": 'Traceback\nFile "src/failure.py", line 3'},
                    {"action": "apply_patch", "args": "diff --git a/src/edit.py b/src/edit.py"},
                    {"action": "read src/late.py"},
                ]
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_trace_artifact(root, "bug-1")

    assert parsed.status == "ok"
    assert parsed.action_steps == 4
    assert parsed.agent_files["src/edit.py"].tier == 4
    assert parsed.agent_files["src/failure.py"].tier == 3
    assert parsed.agent_files["src/a.py"].tier == 2
    assert "src/late.py" not in parsed.agent_files


def test_scheduler_trace_gates_structural_add_and_drops_provisional_anchor(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, file_path TEXT NOT NULL);
        CREATE TABLE edges (source_id INTEGER NOT NULL, target_id INTEGER NOT NULL, confidence REAL);
        INSERT INTO nodes (id, file_path) VALUES
            (1, 'src/edited.py'),
            (2, 'src/neighbor.py'),
            (3, 'src/reject.py');
        INSERT INTO edges (source_id, target_id, confidence) VALUES
            (1, 2, 0.8),
            (1, 3, 0.9);
        """
    )
    conn.close()
    gt = [
        _rec("src/gt1.py", 0.9),
        _rec("src/gt2.py", 0.8),
        _rec("src/gt3.py", 0.7),
        _rec("src/neighbor.py", 0.6, entered_via="graph_rescue", reach=0.5, path_len=1),
        _rec("src/reject.py", 0.5),
        _rec("src/opened.py", 0.4),
    ]
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "bug-2.json").write_text(
        json.dumps(
            [
                {"action": "apply_patch", "args": "diff --git a/src/edited.py b/src/edited.py"},
                {"action": "read src/opened.py"},
                {"action": "rg thing src/opened.py"},
            ]
        ),
        encoding="utf-8",
    )
    parsed = parse_trace_artifact(root, "bug-2")

    result = schedule_v82(gt, str(db), parsed.events, preferred_max=3, hard_ceiling=7)

    assert "src/edited.py" in result.active_files
    assert "src/neighbor.py" in result.active_files
    assert "src/reject.py" not in result.active_files
    assert result.structural_added == ["src/neighbor.py"]
    assert len(result.active_files) <= 3
    assert any(path in {"src/gt1.py", "src/gt2.py"} for path in result.dropped_files)
