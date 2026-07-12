#!/usr/bin/env bash
# Local mandatory-metrics pipeline (CLAUDE.md "Deep per-run logging at 8-dp").
# Downloads a GHA run's per-task artifacts to D: and computes, per task:
#   gt_deep_metrics_<task>.json   (8-dp: agent behavior + substrate-from-cert + lsp + embedder + outcome)
#   gt_metrics_delta_<task>.json  (paired vs the mini-swe GT-off 83/300 baseline)
# No graph.db needed — substrate reads the uploaded graph_certificate.json (gt_deep_metrics cert fallback).
#
# usage: run_local_deep_metrics.sh <run_id> [repo]
set -uo pipefail
RUN_ID="${1:?usage: run_local_deep_metrics.sh <run_id> [repo]}"
REPO="${2:-harneet2512/groundtruth}"
GT="/d/Groundtruth"
ROOT="/d/gt_runs/$RUN_ID"                       # D: has the space; C: does NOT
ART="$ROOT/artifacts"; MET="$ROOT/metrics"
# mini-swe GT-off 83/300 = the TRUE baseline (CLAUDE.md). The [3/3] aggregate needs a real
# resolved_ids LIST; a summary file that only carries a resolved COUNT makes the pairing read an
# empty set (every resolve looks like a flip, 0 regressions). The 83 file has the list. Override
# via GT_BASELINE_RESOLVED.
BASELINE="${GT_BASELINE_RESOLVED:-$GT/.claude/reports/mini_gtoff_baseline_83/resolved_ids.json}"
GTDM="$GT/scripts/swebench/gt_deep_metrics.py"
mkdir -p "$ART" "$MET"

echo "[1/3] downloading task-* + gt-gate-deep-* artifacts of run $RUN_ID -> $ART"
# clear the gh run-log zip cache on C: between batches (it bloats the system drive)
rm -f "/c/Users/Lenovo/AppData/Local/GitHub CLI/run-log-"*.zip 2>/dev/null
for nm in $(gh api "repos/$REPO/actions/runs/$RUN_ID/artifacts" --paginate \
            --jq '.artifacts[].name' | grep -E '^(task-|gt-gate-deep-)' | sort -u); do
  [ -d "$ART/$nm" ] && continue
  gh run download "$RUN_ID" -R "$REPO" -n "$nm" -D "$ART/$nm" >/dev/null 2>&1 \
    && echo "  dl $nm" || echo "  MISS $nm"
done

echo "[2/3] computing per-task deep metrics + paired deltas -> $MET"
n=0
for td in "$ART"/task-*; do
  [ -d "$td" ] || continue
  t="$(basename "$td")"; t="${t#task-}"
  cp "$td"/gt_debug/gt_run_summary_*.json /tmp/ 2>/dev/null || true
  cp "$td"/gt_debug/gt_layer_events_*.jsonl /tmp/ 2>/dev/null || true
  cp "$td"/gt_debug/gt_agent_events_*.jsonl /tmp/ 2>/dev/null || true
  cert="$ART/gt-gate-deep-$t/gt"
  GT_CERT_DIR="$cert" python "$GTDM" "$t" "$td/results" \
     --baseline "$BASELINE" --out "$MET/gt_deep_metrics_$t.json" >/dev/null 2>&1
  cp "/tmp/gt_metrics_delta_$t.json" "$MET/" 2>/dev/null || true
  n=$((n+1))
done
echo "  computed $n tasks"

echo "[3/3] aggregate (resolved + paired) -> $MET/AGGREGATE.json"
python - "$MET" "$BASELINE" <<'PY'
import json,glob,os,sys
met,baseline=sys.argv[1],sys.argv[2]
base=set(json.load(open(baseline)).get("resolved_ids",[]))
rows=[json.load(open(f)) for f in glob.glob(os.path.join(met,"gt_deep_metrics_*.json"))]
res=[r for r in rows if r.get("resolved") is True]
flips=[r["task_id"] for r in rows if r.get("resolved") is True and r["task_id"] not in base]
regr=[r["task_id"] for r in rows if r.get("resolved") is False and r["task_id"] in base]
agg={"tasks":len(rows),"resolved":len(res),
     "flips_vs_baseline":sorted(flips),"flip_count":len(flips),
     "regressions":sorted(regr),"regression_count":len(regr),
     "baseline_resolved":len(base)}
json.dump(agg,open(os.path.join(met,"AGGREGATE.json"),"w"),indent=2)
print(json.dumps(agg,indent=2))
PY
echo "DONE -> $MET"
