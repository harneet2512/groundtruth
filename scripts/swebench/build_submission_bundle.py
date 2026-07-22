#!/usr/bin/env python3
"""Assemble the SWE-bench-Live submission bundle IN-RUN (sealed; never post-run).

Format verified against SWE-bench-Live/submission example
`submissions/lite/20250725-openhands-Qwen3-Coder-480B-A35B/`:

  submissions/lite/{date}-{agent}-{model}/
    preds.json    dict keyed by instance_id -> {instance_id, model_patch, model_name_or_path}
    results.json  schema_version=2 report (total/submitted/completed/resolved/... + *_ids)
    trajs/<id>.json   per-instance native trajectory
    README.md     scaffold + experimental setting

Inputs are the SAME predictions.jsonl the summarize job already builds (build_ll_predictions.py)
plus the ll-full-<task> artifact dirs. results.json is computed from the run's own in-container
FAIL_TO_PASS/PASS_TO_PASS verdict (reward.txt) — the run's sealed evaluation, not a post-hoc score.

Usage:
  build_submission_bundle.py <predictions.jsonl> <artifacts_dir> <out_dir> \
      <model> <agent> <run_id> <date-YYYYMMDD> <substrate_digest>
"""
from __future__ import annotations

import json
import os
import shutil
import sys


def build(preds_jsonl, arts_root, out_dir, model, agent, run_id, date, substrate):
    slug = model.replace("/", "-")
    folder = os.path.join(out_dir, "submissions", "lite", f"{date}-{agent}-{slug}")
    trajs = os.path.join(folder, "trajs")
    os.makedirs(trajs, exist_ok=True)

    # preds.json — dict keyed by instance_id (SWE-bench-Live shape)
    preds = {}
    with open(preds_jsonl, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            preds[r["instance_id"]] = {
                "instance_id": r["instance_id"],
                "model_patch": r.get("model_patch", ""),
                "model_name_or_path": r.get("model_name_or_path", model),
            }
    json.dump(preds, open(os.path.join(folder, "preds.json"), "w", encoding="utf-8"), indent=2)

    # results.json — schema_version 2, from the run's own per-task verdicts
    submitted, completed, resolved, empty_patch, error = [], [], [], [], []
    for iid, rec in preds.items():
        d = os.path.join(arts_root, f"ll-full-{iid}")
        has_traj = os.path.isfile(os.path.join(d, "mini-swe-agent.trajectory.json"))
        patch = (rec.get("model_patch") or "").strip()
        if not os.path.isdir(d) or not has_traj:
            error.append(iid)          # agent never produced a trajectory
            continue
        completed.append(iid)
        submitted.append(iid)
        if not patch:
            empty_patch.append(iid)
        try:
            rew = int(float(open(os.path.join(d, "reward.txt")).read().strip() or 0))
        except Exception:
            rew = 0
        if rew == 1:
            resolved.append(iid)
    unresolved = [i for i in submitted if i not in resolved]
    incomplete = [i for i in preds if i not in completed]
    results = {
        "total_instances": len(preds),
        "submitted_instances": len(submitted),
        "completed_instances": len(completed),
        "resolved_instances": len(resolved),
        "unresolved_instances": len(unresolved),
        "empty_patch_instances": len(empty_patch),
        "error_instances": len(error),
        "completed_ids": sorted(completed),
        "incomplete_ids": sorted(incomplete),
        "empty_patch_ids": sorted(empty_patch),
        "submitted_ids": sorted(submitted),
        "resolved_ids": sorted(resolved),
        "unresolved_ids": sorted(unresolved),
        "error_ids": sorted(error),
        "schema_version": 2,
    }
    json.dump(results, open(os.path.join(folder, "results.json"), "w", encoding="utf-8"), indent=2)

    # trajs/<id>.json
    n_tr = 0
    for iid in preds:
        src = os.path.join(arts_root, f"ll-full-{iid}", "mini-swe-agent.trajectory.json")
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(trajs, f"{iid}.json"))
            n_tr += 1

    with open(os.path.join(folder, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(
            f"# {agent} + {model}\n\n"
            f"**Agent scaffold:** GroundTruth (RL-native evidence layer) over mini-swe-agent.\n\n"
            f"**Model:** {model}\n\n"
            f"**Experimental setting:** 1 rollout per instance (no best-of-N). temperature=1.0, "
            f"top_p=1.0, max_tokens=16384, num_retries=3, step_limit=150, cost_limit=$3.0, "
            f"no wall-time limit, per-command timeout=3600s.\n\n"
            f"**Substrate digest:** `{substrate}`  |  **Run id:** `{run_id}`  |  **Date:** {date}\n\n"
            f"`results.json` is the run's own sealed evaluation (in-container FAIL_TO_PASS/"
            f"PASS_TO_PASS per instance).\n"
        )

    print(f"SUBMISSION BUNDLE @ {folder}")
    print(f"  preds.json: {len(preds)} instances (keyed dict)")
    print(f"  results.json schema_version=2: resolved={len(resolved)} submitted={len(submitted)} "
          f"completed={len(completed)} empty_patch={len(empty_patch)} error={len(error)}")
    print(f"  trajs/: {n_tr}")
    return folder


if __name__ == "__main__":
    a = sys.argv
    if len(a) < 9:
        print("usage: build_submission_bundle.py <preds.jsonl> <arts_dir> <out_dir> "
              "<model> <agent> <run_id> <date> <substrate_digest>", file=sys.stderr)
        raise SystemExit(2)
    build(a[1], a[2], a[3], model=a[4], agent=a[5], run_id=a[6], date=a[7], substrate=a[8])
