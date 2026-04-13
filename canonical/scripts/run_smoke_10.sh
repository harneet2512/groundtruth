#!/bin/bash
# 10-task smoke test for canonical SWE-agent + DeepSeek V3.2 + GT hybrid.
#
# Runs both baseline (no GT) and GT hybrid on the same 10 tasks,
# then analyzes telemetry for go/no-go.
#
# Usage:
#   bash canonical/scripts/run_smoke_10.sh
#
# Prerequisites:
#   - LiteLLM proxy running: litellm --config canonical/config/litellm_vertex_deepseek.yaml
#   - Docker images pulled for these 10 instances
#   - swe-agent installed: pip install swe-agent

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# 10 fixed smoke test instances (mix of difficulty from Live Lite)
# These should be replaced with actual verified Live Lite instance IDs
# once the dataset is loaded and triaged.
INSTANCE_IDS="astropy__astropy-12907,django__django-11133,django__django-11179,django__django-13757,matplotlib__matplotlib-22711,pytest-dev__pytest-5262,scikit-learn__scikit-learn-11040,sphinx-doc__sphinx-7454,sympy__sympy-15976,sympy__sympy-18698"

BASELINE_DIR="$REPO_ROOT/results/smoke_${TIMESTAMP}/baseline"
GT_DIR="$REPO_ROOT/results/smoke_${TIMESTAMP}/gt_hybrid"

echo "================================================================"
echo "CANONICAL SMOKE TEST (10 tasks)"
echo "================================================================"
echo "Timestamp:    $TIMESTAMP"
echo "Instances:    $INSTANCE_IDS"
echo "Baseline dir: $BASELINE_DIR"
echo "GT dir:       $GT_DIR"
echo "================================================================"

# Pre-flight checks
echo ""
echo "[1/5] Pre-flight checks..."

# Check LiteLLM proxy
if ! curl -sf http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: LiteLLM proxy not running at localhost:4000"
    echo "Start it: litellm --config canonical/config/litellm_vertex_deepseek.yaml"
    exit 1
fi
echo "  LiteLLM proxy: OK"

# Check Docker
if ! docker info > /dev/null 2>&1; then
    echo "ERROR: Docker not available"
    exit 1
fi
echo "  Docker: OK"

# Check disk space (need at least 20GB for 10-task smoke)
FREE_GB=$(df -BG / 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G' || echo "unknown")
echo "  Free disk: ${FREE_GB}GB"
if [ "$FREE_GB" != "unknown" ] && [ "$FREE_GB" -lt 20 ]; then
    echo "WARNING: Less than 20GB free. Consider resizing disk."
fi

# Check gt-index binary
GT_INDEX=$(find "$REPO_ROOT/gt-index" -name "gt-index-*" -type f 2>/dev/null | head -1 || true)
if [ -z "$GT_INDEX" ]; then
    echo "WARNING: gt-index binary not found in $REPO_ROOT/gt-index/"
    echo "  GT injection will fail. Build or download gt-index first."
fi
echo "  gt-index: ${GT_INDEX:-NOT FOUND}"

echo ""
echo "[2/5] Running BASELINE (no GT) on 10 tasks..."
echo "  Command: python canonical/scripts/run_baseline.py"

python3 "$REPO_ROOT/canonical/scripts/run_baseline.py" \
    --config "$REPO_ROOT/canonical/config/sweagent_deepseek_v3.2_baseline.yaml" \
    --output-dir "$BASELINE_DIR" \
    --instance-ids "$INSTANCE_IDS" \
    --workers 1 2>&1 | tee "$BASELINE_DIR/run_stdout.log"

echo ""
echo "[3/5] Running GT HYBRID on 10 tasks..."
echo "  Command: python canonical/scripts/run_gt_hybrid.py"

python3 "$REPO_ROOT/canonical/scripts/run_gt_hybrid.py" \
    --config "$REPO_ROOT/canonical/config/sweagent_deepseek_v3.2_gt.yaml" \
    --output-dir "$GT_DIR" \
    --instance-ids "$INSTANCE_IDS" \
    --workers 1 2>&1 | tee "$GT_DIR/run_stdout.log"

echo ""
echo "[4/5] Analyzing GT telemetry..."
python3 "$REPO_ROOT/canonical/scripts/analyze_telemetry.py" \
    --input-dir "$GT_DIR" --verbose

echo ""
echo "[5/5] Comparison summary..."

BASELINE_PATCHES=$(wc -l < "$BASELINE_DIR/submission/all_preds.jsonl" 2>/dev/null || echo "0")
GT_PATCHES=$(wc -l < "$GT_DIR/submission/all_preds.jsonl" 2>/dev/null || echo "0")

echo ""
echo "================================================================"
echo "SMOKE TEST RESULTS"
echo "================================================================"
echo "  Baseline patches: $BASELINE_PATCHES / 10"
echo "  GT hybrid patches: $GT_PATCHES / 10"
echo ""
echo "  Baseline output: $BASELINE_DIR"
echo "  GT hybrid output: $GT_DIR"
echo "  Telemetry report: $GT_DIR/telemetry_report.json"
echo ""
echo "Next steps:"
echo "  1. Evaluate both with SWE-bench harness"
echo "  2. Compare resolved counts"
echo "  3. Check telemetry_report.json for go/no-go"
echo "  4. If PASS: run full 300-task batch"
echo "================================================================"
