#!/usr/bin/env bash
# GT Format A/B Test — 5 tasks, baseline vs GT hooked, heavy logging
# Uses gemini-3-flash-preview via Vertex AI global endpoint
set -euo pipefail

export VERTEXAI_PROJECT="fit-parity-491905-t9"
export VERTEXAI_LOCATION="global"

source "$HOME/gt-venv/bin/activate"
cd "$HOME/groundtruth"

MODEL="vertex_ai/gemini-3-flash-preview"
# 5 diverse tasks from diagnostic set
TASKS="django__django-12856,django__django-11049,django__django-13964,sympy__sympy-14774,scikit-learn__scikit-learn-14092"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTDIR="$HOME/eval_ab_${TIMESTAMP}"
mkdir -p "$OUTDIR"

CONFIG="benchmarks/swebench/mini_swebench_verified_gt_v13_g3f.yaml"

echo "============================================"
echo "  GT FORMAT A/B TEST — gemini-3-flash-preview"
echo "  Branch: $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"
echo "  Output: $OUTDIR"
echo "============================================"

# Step 1: Build Docker images
echo ""
echo "=== Step 1: Building Docker images ==="
python3 -m swebench.harness.prepare_images \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --split test \
  --instance_ids ${TASKS//,/ } \
  --max_workers 2 \
  2>&1 | tee "$OUTDIR/docker_build.log"

# Step 2: BASELINE (no GT)
echo ""
echo "=== Step 2: BASELINE run ==="
python3 -m minisweagent.run.benchmarks.swebench \
  -c "$CONFIG" \
  --model "$MODEL" \
  --subset princeton-nlp/SWE-bench_Verified \
  --split test \
  --instance-ids "$TASKS" \
  -w 2 \
  --output-dir "$OUTDIR/baseline" \
  2>&1 | tee "$OUTDIR/baseline_run.log"

# Step 3: GT HOOKED (new format)
echo ""
echo "=== Step 3: GT HOOKED run ==="
python3 benchmarks/swebench/run_mini_gt_hooked.py swebench \
  -c "$CONFIG" \
  --model "$MODEL" \
  --subset princeton-nlp/SWE-bench_Verified \
  --split test \
  --instance-ids "$TASKS" \
  -w 2 \
  --output-dir "$OUTDIR/gt_hooked" \
  2>&1 | tee "$OUTDIR/gt_hooked_run.log"

# Step 4: Analyze GT evidence in trajectories
echo ""
echo "=== Step 4: GT Evidence Analysis ==="
python3 scripts/swebench/analyze_ab.py "$OUTDIR" 2>&1 | tee "$OUTDIR/analysis.log"

echo ""
echo "=== COMPLETE ==="
echo "Results in: $OUTDIR"
ls -la "$OUTDIR/"
