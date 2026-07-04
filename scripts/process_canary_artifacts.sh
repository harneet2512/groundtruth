#!/usr/bin/env bash
# Pull GHA canary artifacts for the 3 dispatched arms and build
# CANARY_COMPARISON.md. Call AFTER all 3 runs are in "completed" state.
#
# Usage: process_canary_artifacts.sh <baseline_run_id> <old_gt_run_id> <v2_live_run_id>

set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <baseline_run_id> <old_gt_run_id> <v2_live_run_id>"
  exit 1
fi

BL_RUN="$1"
OG_RUN="$2"
V2_RUN="$3"

OUT="canary_artifacts"
rm -rf "$OUT" && mkdir -p "$OUT/baseline" "$OUT/old_gt" "$OUT/v2_live"

echo "==> downloading baseline run $BL_RUN"
gh run download "$BL_RUN"  --dir "$OUT/baseline"
echo "==> downloading old_gt run $OG_RUN"
gh run download "$OG_RUN"  --dir "$OUT/old_gt"
echo "==> downloading v2_live run $V2_RUN"
gh run download "$V2_RUN"  --dir "$OUT/v2_live"

echo "==> directory layout"
find "$OUT" -maxdepth 4 -type d | head -40

echo "==> running compute_canary_metrics"
python scripts/compute_canary_metrics.py \
  --baseline "$OUT/baseline" \
  --old-gt   "$OUT/old_gt" \
  --v2       "$OUT/v2_live" \
  --repo-root /workspace \
  --report   reports/canary/CANARY_COMPARISON.md \
  --json     reports/canary/canary_metrics.json

echo "==> done. Read reports/canary/CANARY_COMPARISON.md"
