#!/usr/bin/env bash
# aggregate_run_local.sh — emit the run-level aggregate for an ALREADY-DOWNLOADED run dir.
#
# WHY THIS EXISTS (Cluster-5 ITEM 1 D1, 2026-07-18): the per-task emitters (gt_deep_metrics /
# gt_feature_metrics) run per task, but the RUN-LEVEL aggregate — gt_run_metrics.v2 +
# gt.feature_effects.v1 + gt.metric_integrity.v1 (+ the SS-LIVE diagnosis) — was only reachable via
# run_local_deep_metrics.sh (which FORCES a `gh` download of a remote run) or the live workflow's
# summarize job. A run whose ll-full-* task dirs already sit on disk (e.g. D:/gt_runs/<run>) had NO
# fail-loud path to the aggregate, so the aggregate was simply never emitted for it.
#
# This wires that aggregate call into the local collect path. It REUSES the three canonical
# authorities unchanged (gt_run_metrics.py, gt_feature_metrics.py, ss_live_diagnosis.py) and is
# FAIL-LOUD: `set -euo pipefail` aborts on any authority's non-zero exit (gt_feature_metrics returns
# 3 on an integrity/publishability failure, gt_run_metrics returns 1 on missing mandatory metrics),
# and the population bind below fails closed with a named reason on any missing/duplicate task.
#
# usage: aggregate_run_local.sh <run_dir> [run_id]
#   <run_dir>  directory holding ll-full-<task> subdirs (each with a mini trajectory +
#              gt_deep_metrics_<task>.json). Aggregates in place; writes to <run_dir>/metrics.
#   [run_id]   defaults to basename(<run_dir>).
set -euo pipefail

RUN_DIR="${1:?usage: aggregate_run_local.sh <run_dir> [run_id]}"
RUN_ID="${2:-$(basename "$(cd "$RUN_DIR" && pwd)")}"
GT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MET="$RUN_DIR/metrics"
EXPECTED_TASKS="$MET/expected_tasks.json"
RUN_METRICS="$MET/gt_run_metrics_v2_${RUN_ID}.json"
export PYTHONPATH="$GT:$GT/src:$GT/scripts/swebench:${PYTHONPATH:-}"

[ -d "$RUN_DIR" ] || { echo "FATAL: run dir not found: $RUN_DIR" >&2; exit 1; }
mkdir -p "$MET"

echo "[1/4] binding local task population from $RUN_DIR"
python - "$RUN_DIR" "$EXPECTED_TASKS" <<'PY'
import json
import os
import sys
from pathlib import Path

run_dir, out = sys.argv[1], sys.argv[2]
tasks = []
for name in sorted(os.listdir(run_dir)):
    d = Path(run_dir) / name
    if not (d.is_dir() and name.startswith("ll-full-")):
        continue
    task = name[len("ll-full-"):]
    if not (d / "mini-swe-agent.trajectory.json").is_file():
        raise SystemExit(f"FATAL: {name}: missing mini trajectory")
    if not (d / f"gt_deep_metrics_{task}.json").is_file():
        raise SystemExit(f"FATAL: {name}: missing gt_deep_metrics_{task}.json")
    tasks.append(task)
if not tasks:
    raise SystemExit(f"FATAL: no ll-full-* task dirs with a mini trajectory under {run_dir}")
if len(tasks) != len(set(tasks)):
    raise SystemExit("FATAL: duplicate task ids in the local population")
Path(out).write_text(json.dumps({"task_ids": tasks}, indent=2), encoding="utf-8")
print(f"  bound {len(tasks)} tasks")
PY

echo "[2/4] canonical 58-PERF run metrics (gt_run_metrics.v2) -> $RUN_METRICS"
python "$GT/scripts/swebench/gt_run_metrics.py" "$RUN_DIR" \
  --run-id "$RUN_ID" \
  --expected-tasks-file "$EXPECTED_TASKS" \
  --output "$RUN_METRICS"

echo "[3/4] exact-129 feature metrics (feature_effects + metric_integrity) -> $MET"
python "$GT/scripts/swebench/gt_feature_metrics.py" "$RUN_DIR" \
  --profile 2 \
  --run-id "$RUN_ID" \
  --expected-tasks-file "$EXPECTED_TASKS" \
  --run-metrics-artifact "$RUN_METRICS" \
  --out "$MET"

echo "[4/4] SS-LIVE diagnosis -> $MET"
python "$GT/scripts/swebench/ss_live_diagnosis.py" "$RUN_DIR" \
  --run-metrics "$RUN_METRICS" \
  --expected-tasks "$EXPECTED_TASKS" \
  --output "$MET/ss_live_diagnosis_${RUN_ID}.json"

echo "DONE -> $MET"
