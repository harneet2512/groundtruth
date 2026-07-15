"""Process-level determinism pins for mandatory scope metrics."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SWE = ROOT / "scripts" / "swebench"


def _run_performance_emitter(
    *, mode: str, hash_seed: int, task_dir: Path, gold_jsonl: Path
) -> bytes:
    script = r"""
import json
import os
import sys

sys.path.insert(0, os.environ["GT_TEST_SWE"])
import gt_deep_metrics
import gt_performance_metrics

task = os.environ["GT_TEST_TASK"]
task_dir = os.environ["GT_TEST_TASK_DIR"]
gold_jsonl = os.environ["GT_GOLD_JSONL"]
if sys.argv[1] == "deep":
    performance = gt_deep_metrics.build(task, task_dir)["performance"]
else:
    performance = gt_performance_metrics.compute_performance_metrics(
        os.path.join(task_dir, "mini-swe-agent.trajectory.json"),
        task_dir,
        instance_id=task,
        gold_jsonl=gold_jsonl,
    )
sys.stdout.buffer.write(json.dumps(
    performance,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8"))
"""
    env = os.environ.copy()
    env.update({
        "PYTHONHASHSEED": str(hash_seed),
        "GT_TEST_SWE": str(SWE),
        "GT_TEST_TASK": task_dir.name,
        "GT_TEST_TASK_DIR": str(task_dir),
        "GT_GOLD_JSONL": str(gold_jsonl),
    })
    return subprocess.run(
        [sys.executable, "-c", script, mode],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout


def test_deep_and_standalone_performance_are_byte_identical_across_hash_seeds(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "acme__widget-1"
    task_dir.mkdir()
    (task_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({"messages": [], "info": {"submission": ""}}),
        encoding="utf-8",
    )
    gold_paths = [
        "src/alpha.py",
        "src/bravo.py",
        "src/charlie.py",
        "src/delta.py",
        "src/echo.py",
        "src/foxtrot.py",
        "src/golf.py",
        "src/hotel.py",
    ]
    patch = "".join(
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        for path in gold_paths
    )
    gold_jsonl = tmp_path / "gold.jsonl"
    gold_jsonl.write_text(
        json.dumps({"instance_id": task_dir.name, "patch": patch}) + "\n",
        encoding="utf-8",
    )

    deep_bytes = _run_performance_emitter(
        mode="deep", hash_seed=1, task_dir=task_dir, gold_jsonl=gold_jsonl
    )
    standalone_bytes = _run_performance_emitter(
        mode="standalone", hash_seed=2, task_dir=task_dir, gold_jsonl=gold_jsonl
    )

    assert deep_bytes == standalone_bytes
    assert json.loads(deep_bytes)["scope_completeness"]["_gold_gap_list"] == sorted(
        gold_paths
    )
