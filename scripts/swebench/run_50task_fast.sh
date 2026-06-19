#!/bin/bash
set -euo pipefail

# Fast 50-task A/B: n_critic_runs=1, max_retries=1, workers=8, max_iterations=30
# Expected: ~2h total instead of ~10h

export PATH="/root/.local/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="/root/oh-benchmarks"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_ROOT="/root/results/v8_fast_${TIMESTAMP}"
SELECT_FILE="/tmp/runnable_50_real.txt"
LLM_CONFIG="$SCRIPT_DIR/oh_llm_config_qwen3_proxy.json"

# Verify select file
if [ ! -f "$SELECT_FILE" ]; then
    echo "ERROR: $SELECT_FILE not found. Run pull_50_images.py first."
    exit 1
fi
TASK_COUNT=$(wc -l < "$SELECT_FILE")

# Verify proxy
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running on port 4000"
    exit 1
fi

# Copy prompt template
cp "$SCRIPT_DIR/oh_prompt_gt_v7.j2" "$OH_DIR/benchmarks/swebench/prompts/gt_hook_v7.j2"

mkdir -p "$OUTPUT_ROOT"

echo "================================================="
echo "  FAST 50-task A/B (v8 precompute)"
echo "  $(date -u) UTC"
echo "  Tasks:     $TASK_COUNT"
echo "  Workers:   6"
echo "  MaxIter:   30"
echo "  Critics:   1"
echo "  Retries:   2"
echo "  Output:    $OUTPUT_ROOT"
echo "================================================="

# ── Common args ──────────────────────────────────────────────────────
COMMON_ARGS=(
    --dataset princeton-nlp/SWE-bench_Lite
    --split test
    --workspace docker
    --max-iterations 30
    --num-workers 6
    --n-critic-runs 1
    --max-retries 2
    --select "$SELECT_FILE"
)

# ── BASELINE ─────────────────────────────────────────────────────────
BASELINE_DIR="$OUTPUT_ROOT/baseline"
mkdir -p "$BASELINE_DIR"

echo ""
echo "─── BASELINE ($TASK_COUNT tasks, 8 workers) ───"
echo "  $(date -u) UTC"

cd "$OH_DIR"
.venv/bin/python -m benchmarks.swebench.run_infer "$LLM_CONFIG" \
    "${COMMON_ARGS[@]}" \
    --output-dir "$BASELINE_DIR" \
    --note "v8_fast_baseline" \
    2>&1 | tee "$BASELINE_DIR/run.log" || true

echo "Baseline done: $(date -u) UTC"

# ── GT V8 PRECOMPUTE ─────────────────────────────────────────────────
GT_DIR="$OUTPUT_ROOT/gt_v8"
GT_LOG_DIR="$GT_DIR/gt_logs"
mkdir -p "$GT_DIR" "$GT_LOG_DIR"
export GT_LOG_DIR

echo ""
echo "─── GT V8 ($TASK_COUNT tasks, 8 workers) ───"
echo "  $(date -u) UTC"

cd "$OH_DIR"
.venv/bin/python "$SCRIPT_DIR/oh_gt_v8_wrapper.py" "$LLM_CONFIG" \
    "${COMMON_ARGS[@]}" \
    --prompt-path gt_hook_v7.j2 \
    --output-dir "$GT_DIR" \
    --note "v8_fast_gt" \
    2>&1 | tee "$GT_DIR/run.log" || true

echo "GT done: $(date -u) UTC"

# ── EVAL ─────────────────────────────────────────────────────────────
echo ""
echo "─── EVALUATING ───"

BASELINE_OUTPUT=$(find "$BASELINE_DIR" -name "output.jsonl" | head -1)
GT_OUTPUT=$(find "$GT_DIR" -name "output.jsonl" | head -1)

if [ -n "$BASELINE_OUTPUT" ]; then
    echo "Eval baseline: $BASELINE_OUTPUT"
    cd "$OH_DIR"
    .venv/bin/python -m benchmarks.swebench.eval_infer "$BASELINE_OUTPUT" \
        --run-id v8_fast_baseline --dataset princeton-nlp/SWE-bench_Lite \
        --split test --no-modal --workers 4 \
        2>&1 | tail -5
fi

if [ -n "$GT_OUTPUT" ]; then
    echo "Eval GT: $GT_OUTPUT"
    cd "$OH_DIR"
    .venv/bin/python -m benchmarks.swebench.eval_infer "$GT_OUTPUT" \
        --run-id v8_fast_gt --dataset princeton-nlp/SWE-bench_Lite \
        --split test --no-modal --workers 4 \
        2>&1 | tail -5
fi

# ── RESULTS ──────────────────────────────────────────────────────────
echo ""
echo "================================================="
echo "  RESULTS"
echo "  $(date -u) UTC"
echo "================================================="

python3 -c "
import json, glob, os

for label, pattern in [('BASELINE', '$BASELINE_DIR/**/output.report.json'), ('GT V8', '$GT_DIR/**/output.report.json')]:
    files = glob.glob(pattern, recursive=True)
    if not files:
        print(f'{label}: NO REPORT')
        continue
    d = json.load(open(files[0]))
    resolved = d.get('resolved_ids', [])
    unresolved = d.get('unresolved_ids', [])
    total = len(resolved) + len(unresolved) + len(d.get('error_ids', []))
    print(f'{label}: {len(resolved)}/{total} resolved ({100*len(resolved)/max(total,1):.1f}%)')
    print(f'  Resolved: {sorted(resolved)}')
    if label == 'GT V8':
        # Show understand calls
        log_dir = '$GT_LOG_DIR'
        if os.path.isdir(log_dir):
            for f in sorted(glob.glob(os.path.join(log_dir, '*.jsonl'))):
                print(f'  Hook log: {os.path.basename(f)} ({os.path.getsize(f)} bytes)')
" 2>/dev/null || true

echo ""
echo "================================================="
echo "  ALL DONE: $(date -u) UTC"
echo "================================================="
