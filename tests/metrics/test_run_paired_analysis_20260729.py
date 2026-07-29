"""run_paired_analysis wires the paired analyzer to real run layouts — task #30 R2.

THE DEFECT THIS PINS OUT (2026-07-29). scripts/metrics/compute_paired_metrics.py is a
complete paired analyzer (locked union + named missingness, E1 absolute_pp, E3 net
tokens, seeded bootstrap, McNemar, Holm) that was INVOKED BY NOTHING that understands
the real arm layouts: the GT-off frozen arm lives in {half0,half1}/ll-full-<task>/ and
the locked task sets live in a txt + a json file. This test proves the runner:
  1. loads the half-split, ll-full-prefixed off arm and a flat on arm via
     compute_paired_metrics.load_run (reused, not reimplemented),
  2. writes paired_report_<timestamp>.json,
  3. reports a locked task-set id absent from BOTH arms as missing
     (absent_from_both_arms) instead of silently vanishing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "scripts" / "metrics",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import run_paired_analysis as rpa  # noqa: E402


def _write_task_dir(task_dir: Path, resolved: bool, tokens_in: float = 1000.0) -> None:
    """Minimal synthetic task dir that compute_task_metrics can load."""
    task_dir.mkdir(parents=True, exist_ok=True)
    traj = {
        "messages": [
            {"role": "user", "content": "issue text"},
            {"role": "assistant", "content": "look around", "tool_calls": []},
        ],
        "info": {
            "submission": "",
            "model_stats": {"api_calls": 1},
        },
    }
    (task_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps(traj), encoding="utf-8"
    )
    outcome = {"tasks": [{"reward": 1 if resolved else 0, "n_agent_steps": 1}]}
    (task_dir / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
    dm = {"efficiency": {"llm_tokens_in": tokens_in, "llm_tokens_out": 100.0}}
    (task_dir / f"gt_deep_metrics_{task_dir.name}.json").write_text(
        json.dumps(dm), encoding="utf-8"
    )


def _build_arms(tmp_path: Path) -> tuple[Path, Path]:
    """On arm = flat task dirs. Off arm = frozen {half0,half1}/ll-full-<task> layout."""
    on_dir = tmp_path / "on"
    _write_task_dir(on_dir / "repo__proj-1", resolved=True, tokens_in=900.0)
    _write_task_dir(on_dir / "repo__proj-2", resolved=False, tokens_in=800.0)

    off_dir = tmp_path / "off"
    _write_task_dir(off_dir / "half0" / "ll-full-repo__proj-1", resolved=False)
    _write_task_dir(off_dir / "half1" / "ll-full-repo__proj-2", resolved=False)
    return on_dir, off_dir


def test_load_arm_walks_halves_and_strips_ll_full_prefix(tmp_path):
    on_dir, off_dir = _build_arms(tmp_path)

    off_run = rpa.load_arm(off_dir)
    assert set(off_run) == {"repo__proj-1", "repo__proj-2"}
    assert all(tm.task_id == tid for tid, tm in off_run.items())

    on_run = rpa.load_arm(on_dir)
    assert set(on_run) == {"repo__proj-1", "repo__proj-2"}


def test_end_to_end_writes_timestamped_report_with_headline(tmp_path, capsys):
    on_dir, off_dir = _build_arms(tmp_path)
    out_dir = tmp_path / "reports"

    rc = rpa.main([
        "--on-dir", str(on_dir),
        "--off-dir", str(off_dir),
        "--output-dir", str(out_dir),
    ])
    assert rc == 0

    reports = sorted(out_dir.glob("paired_report_*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))

    assert set(report["per_task"]) == {"repo__proj-1", "repo__proj-2"}
    headline = report["headline"]
    # off 0/2 resolved, on 1/2 resolved -> +50 pp, 1 flip, 0 regressions
    assert headline["absolute_resolution_pp"] == 50.0
    assert headline["flip_count"] == 1
    assert headline["regression_count"] == 0
    # headline is printed for the operator
    printed = capsys.readouterr().out
    assert "absolute_resolution_pp" in printed
    assert "flip" in printed


def test_task_set_id_absent_from_both_arms_is_named_not_vanished(tmp_path):
    on_dir, off_dir = _build_arms(tmp_path)
    out_dir = tmp_path / "reports"
    task_set = tmp_path / "locked.txt"
    task_set.write_text(
        "# locked set\nrepo__proj-1\nrepo__proj-2\nghost__task-99\n",
        encoding="utf-8",
    )

    rc = rpa.main([
        "--on-dir", str(on_dir),
        "--off-dir", str(off_dir),
        "--task-set", str(task_set),
        "--output-dir", str(out_dir),
    ])
    assert rc == 0

    report = json.loads(
        sorted(out_dir.glob("paired_report_*.json"))[0].read_text(encoding="utf-8")
    )
    pop = report["population"]
    ghost = pop["missing_tasks"]["ghost__task-99"]
    assert ghost["in_baseline"] is False
    assert ghost["in_oracle"] is False
    assert ghost["missing_reason"] == "absent_from_both_arms"
    assert pop["task_set"]["n_locked"] == 3
    assert pop["task_set"]["missing_from_both_arms"] == ["ghost__task-99"]
    # the paired population is still only what both arms measured
    assert set(report["per_task"]) == {"repo__proj-1", "repo__proj-2"}


def test_task_set_restricts_population_to_locked_ids(tmp_path):
    on_dir, off_dir = _build_arms(tmp_path)
    # an extra task in both arms that is NOT in the locked set must be excluded
    _write_task_dir(on_dir / "extra__task-7", resolved=True)
    _write_task_dir(off_dir / "half0" / "ll-full-extra__task-7", resolved=False)
    out_dir = tmp_path / "reports"
    task_set = tmp_path / "locked.txt"
    task_set.write_text("repo__proj-1\nrepo__proj-2\n", encoding="utf-8")

    rc = rpa.main([
        "--on-dir", str(on_dir),
        "--off-dir", str(off_dir),
        "--task-set", str(task_set),
        "--output-dir", str(out_dir),
    ])
    assert rc == 0
    report = json.loads(
        sorted(out_dir.glob("paired_report_*.json"))[0].read_text(encoding="utf-8")
    )
    assert "extra__task-7" not in report["per_task"]
    assert "extra__task-7" not in report["population"]["missing_tasks"]


def test_load_task_set_reads_txt_and_blind_json(tmp_path):
    txt = tmp_path / "bag.txt"
    txt.write_text("# comment\nrepo__a-1\n\nrepo__b-2\n# trailer\n", encoding="utf-8")
    blind = tmp_path / "blind.json"
    blind.write_text(json.dumps({
        "version": "blind-ext-selector-vFINAL",
        "selected_signature": ["repo__c-3"],
        "selected_newfile": ["repo__d-4", "repo__b-2"],
        "sig_pool": ["repo__ignored-9"],
    }), encoding="utf-8")

    ids = rpa.load_task_set([txt, blind])
    assert ids == {"repo__a-1", "repo__b-2", "repo__c-3", "repo__d-4"}
