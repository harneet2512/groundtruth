from __future__ import annotations

import json
from pathlib import Path

from groundtruth.cli.commands import gt_plan_cmd
from groundtruth.runtime.plan_surface import compact_plan, usable_delivery_record


def test_compact_plan_excludes_cluster_files_and_stays_small() -> None:
    plan = {
        "task_id": "task-1",
        "confidence": 0.82,
        "abstain_reason": "",
        "cluster_files": [f"pkg/file_{idx}.py" for idx in range(20)],
        "agent_focus_files": [{"file": f"pkg/file_{idx}.py"} for idx in range(5)],
        "contract_lines": [f"contract {idx}" for idx in range(10)],
        "constraints": [f"constraint {idx}" for idx in range(10)],
        "expected_side_files": [f"tests/test_{idx}.py" for idx in range(10)],
    }

    compact = compact_plan(plan)
    rendered = json.dumps(compact, sort_keys=True)

    assert "cluster_files" not in compact
    assert compact["full_plan_available"] is True
    assert len(compact["agent_focus_files"]) == 3
    assert len(compact["contract_lines"]) == 2
    assert len(compact["constraints"]) == 3
    assert len(compact["expected_side_files"]) == 3
    assert len(rendered) < 3500


def test_usable_delivery_gate_rejects_broad_or_large_payloads() -> None:
    too_many_focus = usable_delivery_record(
        transport_delivered=True,
        brief_chars=120,
        agent_focus_files=[{"file": "a.py"}, {"file": "b.py"}, {"file": "c.py"}, {"file": "d.py"}],
        brief_text="edit a.py and b.py",
    )
    too_large = usable_delivery_record(
        transport_delivered=True,
        brief_chars=3501,
        agent_focus_files=[{"file": "a.py"}],
    )
    full_default = usable_delivery_record(
        transport_delivered=True,
        brief_chars=120,
        agent_focus_files=[{"file": "a.py"}],
        broad_full_plan_default=True,
    )

    assert too_many_focus["usable_delivery_ok"] is False
    assert too_many_focus["brief_file_mentions_count"] == 2
    assert "too_many_focus_files" in too_many_focus["failure_reasons"]
    assert "brief_too_large" in too_large["failure_reasons"]
    assert "broad_full_plan_default" in full_default["failure_reasons"]


def test_gt_plan_cli_compact_default_and_full_diagnostic(tmp_path: Path, capsys) -> None:
    plan_path = tmp_path / "task_v7_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "task_id": "task-1",
                "cluster_files": ["pkg/core.py"],
                "agent_focus_files": [{"file": "pkg/core.py"}],
                "contract_lines": ["line 1"],
                "constraints": ["constraint"],
                "expected_side_files": ["tests/test_core.py"],
            }
        ),
        encoding="utf-8",
    )

    gt_plan_cmd(plan_path=str(plan_path))
    compact = json.loads(capsys.readouterr().out)
    gt_plan_cmd(plan_path=str(plan_path), full=True)
    full = json.loads(capsys.readouterr().out)

    assert "cluster_files" not in compact
    assert compact["full_plan_available"] is True
    assert full["cluster_files"] == ["pkg/core.py"]
