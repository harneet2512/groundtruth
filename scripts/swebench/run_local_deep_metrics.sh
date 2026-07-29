#!/usr/bin/env bash
# Download and rebuild the canonical run-level PERF + exact-128 diagnosis bundle.
# The per-task ll-full artifacts are the immutable live population; this script
# aggregates them locally without launching either a GT-off or a paid run.
#
# usage: run_local_deep_metrics.sh <run_id> [repo]
set -euo pipefail

RUN_ID="${1:?usage: run_local_deep_metrics.sh <run_id> [repo]}"
REPO="${2:-harneet2512/groundtruth}"
GT="/d/Groundtruth"
ROOT="/d/gt_runs/$RUN_ID"
ART="$ROOT/artifacts"
POP="$ART"
MET="$ROOT/metrics"
DIAG="$ART/gt-diagnosis-$RUN_ID"
EXPECTED_TASKS="$DIAG/expected_tasks.json"
RUN_METRICS="$MET/gt_run_metrics_v2_${RUN_ID}.json"
export PYTHONPATH="$GT:$GT/src:$GT/scripts/swebench:${PYTHONPATH:-}"

mkdir -p "$ART" "$MET"

echo "[1/5] downloading canonical ll-full + diagnosis artifacts for run $RUN_ID"
# Clear the gh run-log zip cache on C: between batches (it bloats the system drive).
rm -f "/c/Users/Lenovo/AppData/Local/GitHub CLI/run-log-"*.zip 2>/dev/null || true
ARTIFACT_LIST="$MET/.canonical_artifact_names"
gh api "repos/$REPO/actions/runs/$RUN_ID/artifacts" --paginate \
  --jq '.artifacts[].name' \
  | grep -E "^(ll-full-|gt-diagnosis-$RUN_ID$)" \
  | sort -u >"$ARTIFACT_LIST"
mapfile -t ARTIFACT_NAMES <"$ARTIFACT_LIST"
rm -f "$ARTIFACT_LIST"
if [ "${#ARTIFACT_NAMES[@]}" -eq 0 ]; then
  echo "FATAL: run $RUN_ID has no canonical ll-full/diagnosis artifacts" >&2
  exit 1
fi
for name in "${ARTIFACT_NAMES[@]}"; do
  [ -d "$ART/$name" ] && continue
  gh run download "$RUN_ID" -R "$REPO" -n "$name" -D "$ART/$name" >/dev/null
  echo "  downloaded $name"
done

echo "[2/5] binding the exact per-task population"
if [ ! -s "$EXPECTED_TASKS" ]; then
  echo "FATAL: canonical expected task population missing: $EXPECTED_TASKS" >&2
  exit 1
fi
TASK_LIST="$MET/.canonical_task_population"
python - "$EXPECTED_TASKS" "$POP" >"$TASK_LIST" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

manifest = Path(sys.argv[1])
population_root = Path(sys.argv[2])
payload = json.loads(manifest.read_text(encoding="utf-8"))
expected = payload.get("task_ids") if isinstance(payload, dict) else None
if (
    not isinstance(expected, list)
    or not expected
    or any(not isinstance(task, str) or not task for task in expected)
    or len(expected) != len(set(expected))
):
    raise SystemExit(f"FATAL: malformed expected task population: {manifest}")

observed = [
    path.name.removeprefix("ll-full-")
    for path in sorted(population_root.glob("ll-full-*"))
    if path.is_dir()
]
counts = Counter(observed)
missing = [task for task in expected if task not in counts]
unexpected = sorted(set(observed) - set(expected))
duplicate = sorted(task for task, count in counts.items() if count != 1)
if missing or unexpected or duplicate or len(observed) != len(expected):
    raise SystemExit(
        "FATAL: canonical task population mismatch: "
        f"missing={missing} unexpected={unexpected} duplicate={duplicate} "
        f"expected={len(expected)} observed={len(observed)}"
    )
for task in expected:
    task_dir = population_root / f"ll-full-{task}"
    required = (
        task_dir / "mini-swe-agent.trajectory.json",
        task_dir / f"gt_deep_metrics_{task}.json",
        task_dir / "gt_task_completion.json",
    )
    absent = [str(path) for path in required if not path.is_file()]
    if absent:
        raise SystemExit(f"FATAL: canonical task artifacts missing for {task}: {absent}")
    print(task)
PY
mapfile -t EXPECTED_TASK_IDS <"$TASK_LIST"
rm -f "$TASK_LIST"
echo "  bound ${#EXPECTED_TASK_IDS[@]} tasks"

echo "[3/5] canonical 58-PERF run metrics -> $RUN_METRICS"
python "$GT/scripts/swebench/gt_run_metrics.py" "$POP" \
  --run-id "$RUN_ID" \
  --expected-tasks-file "$EXPECTED_TASKS" \
  --output "$RUN_METRICS"

echo "[4/5] exact-128 feature metrics -> $MET"
python "$GT/scripts/swebench/gt_feature_metrics.py" "$POP" \
  --profile 2 \
  --run-id "$RUN_ID" \
  --expected-tasks-file "$EXPECTED_TASKS" \
  --run-metrics-artifact "$RUN_METRICS" \
  --out "$MET"

echo "[5/5] SS-LIVE diagnosis -> $MET"
python "$GT/scripts/swebench/ss_live_diagnosis.py" "$POP" \
  --run-metrics "$RUN_METRICS" \
  --expected-tasks "$EXPECTED_TASKS" \
  --output "$MET/ss_live_diagnosis_${RUN_ID}.json"
python "$GT/scripts/swebench/ss_live_diagnosis.py" "$POP" \
  --run-metrics "$RUN_METRICS" \
  --expected-tasks "$EXPECTED_TASKS" \
  --format markdown \
  --output "$MET/ss_live_diagnosis_${RUN_ID}.md"

# [6/6] PAIRED ANALYSIS (2026-07-29): compute_paired_metrics was a complete,
# publication-grade analyzer (locked union, E1 absolute pp, E3 net tokens,
# seeded bootstrap, McNemar, Holm) that NOTHING invoked — the audited R2 gap.
# The GT-off arm defaults to the frozen D:\gt_runs\gtoff_baseline_trajs inside
# run_paired_analysis.py. Non-fatal (|| true): a paired report is analysis, not
# a gate; its absence is visible in the DONE listing, never a silent run-kill.
echo "[6/6] paired GT-on vs frozen GT-off -> $MET"
python "$GT/scripts/metrics/run_paired_analysis.py" \
  --on-dir "$POP" \
  --output-dir "$MET" || echo "WARN: paired analysis failed (see above) — deep metrics remain valid"

echo "DONE -> $MET"
