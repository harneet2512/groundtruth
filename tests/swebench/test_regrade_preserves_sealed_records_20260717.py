"""D9 — an --out re-grade must NEVER mutate the sealed task-dir records.

The in-container per-task collection writes gt_feature_metrics_<task>.json into the
task dir and gt_task_completion.json seals its hash. The summarize diagnosis pass
re-grades the whole run with --out <diag_dir>; before D9 it rewrote the per-task
record IN PLACE, breaking the completion hash binding for every task (live witness:
run 29553735978 — 30/30 graded 'incomplete', publishable=false). Contract:

  * with --out: task-dir bytes are byte-identical after the re-grade; the enriched
    per-task record lands under out_dir.
  * without --out: the task dir IS the output target (the sealed-record creation
    path) — unchanged behavior.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_REPO, "scripts", "swebench"))
sys.path.insert(0, os.path.join(_REPO, "src"))
sys.path.insert(0, _REPO)

import gt_feature_metrics as gfm  # noqa: E402

_SAVED = "D:/gt_runs/validation30_20260717/ll-full-amoffat__sh-744"


def _sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


@pytest.mark.skipif(not os.path.isdir(_SAVED), reason="saved validation artifact absent")
def test_out_regrade_leaves_task_dir_byte_identical(tmp_path) -> None:
    task = "amoffat__sh-744"
    run_dir = tmp_path / "run"
    task_dir = run_dir / f"ll-full-{task}"
    shutil.copytree(_SAVED, task_dir)
    sealed = str(task_dir / f"gt_feature_metrics_{task}.json")
    sealed_md = str(task_dir / f"gt_feature_metrics_{task}.md")
    before = _sha(sealed)
    before_md = _sha(sealed_md) if os.path.isfile(sealed_md) else None

    out_dir = tmp_path / "diag"
    rc = gfm.main([str(run_dir), "--profile", "2", "--run-id", "t", "--out", str(out_dir)])
    # rc 3 = honest unpublishable-aggregate verdict on a synthetic 1-task run without a
    # run-metrics artifact; the contract under test is the WRITE behavior, not the verdict.
    assert rc in (0, 3)

    # RED before D9: the in-place rewrite changed these bytes.
    assert _sha(sealed) == before, "sealed task-dir record was mutated by --out re-grade"
    if before_md is not None:
        assert _sha(sealed_md) == before_md
    # The enriched record must exist under out_dir instead.
    enriched = out_dir / f"gt_feature_metrics_{task}.json"
    assert enriched.is_file()
    assert json.load(open(enriched, encoding="utf-8"))["task"] == task


@pytest.mark.skipif(not os.path.isdir(_SAVED), reason="saved validation artifact absent")
def test_no_out_still_writes_task_dir(tmp_path) -> None:
    task = "amoffat__sh-744"
    run_dir = tmp_path / "run"
    task_dir = run_dir / f"ll-full-{task}"
    shutil.copytree(_SAVED, task_dir)
    sealed = str(task_dir / f"gt_feature_metrics_{task}.json")
    os.remove(sealed)

    rc = gfm.main([str(run_dir), "--profile", "2", "--run-id", "t"])
    assert rc in (0, 3)
    assert os.path.isfile(sealed), "creation path must still write into the task dir"
