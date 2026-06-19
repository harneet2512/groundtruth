#!/bin/bash
set -euo pipefail

# GT v1.1 source-level patch runner for OpenHands v0.44.0 SWE-bench evaluation.
#
# Applies source-level patches to run_infer.py (survives mp.Pool fork),
# sets up GT_HOOK_PATH and GT_LOG_DIR, then runs the evaluation.
#
# Usage:
#   bash oh_gt_v11_run.sh [run_infer.py args...]
#
# Examples:
#   bash oh_gt_v11_run.sh --llm-config llm.json --eval-num-workers 2 \
#       --max-iterations 50 --dataset princeton-nlp/SWE-bench_Verified \
#       --split test --filter-instances django__django-10097
#
# Environment:
#   OH_DIR          -- OpenHands root (default: /root/oh-benchmarks)
#   GT_HOOK_PATH    -- path to gt_hook.py (default: ~/gt_hook.py)
#   GT_LOG_DIR      -- log output dir (default: /tmp/gt_logs)
#   GT_DRY_RUN      -- if set, patch only, don't run

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

OH_DIR="${OH_DIR:-/root/oh-benchmarks}"
GT_HOOK_PATH="${GT_HOOK_PATH:-$HOME/gt_hook.py}"
GT_LOG_DIR="${GT_LOG_DIR:-/tmp/gt_logs}"

RUN_INFER="$OH_DIR/evaluation/benchmarks/swe_bench/run_infer.py"
PATCHER="$SCRIPT_DIR/oh_source_patch.py"

# ── Preflight ────────────────────────────────────────────────────────

echo "================================================="
echo "  GT v1.1 Source Patch Runner"
echo "  $(date -u) UTC"
echo "================================================="
echo ""

if [ ! -d "$OH_DIR" ]; then
    echo "ERROR: OH_DIR not found: $OH_DIR"
    exit 1
fi

if [ ! -f "$RUN_INFER" ]; then
    echo "ERROR: run_infer.py not found: $RUN_INFER"
    exit 1
fi

if [ ! -f "$PATCHER" ]; then
    echo "ERROR: Patcher not found: $PATCHER"
    exit 1
fi

if [ ! -f "$GT_HOOK_PATH" ]; then
    # Try repo copy
    REPO_HOOK="$REPO_DIR/benchmarks/swebench/vm_bundle/gt_hook_vm.py"
    if [ -f "$REPO_HOOK" ]; then
        echo "Copying gt_hook from repo: $REPO_HOOK -> $GT_HOOK_PATH"
        cp "$REPO_HOOK" "$GT_HOOK_PATH"
    else
        echo "ERROR: gt_hook.py not found at $GT_HOOK_PATH or $REPO_HOOK"
        exit 1
    fi
fi

echo "  OH_DIR:       $OH_DIR"
echo "  run_infer.py: $RUN_INFER"
echo "  GT_HOOK_PATH: $GT_HOOK_PATH ($(wc -c < "$GT_HOOK_PATH") bytes)"
echo "  GT_LOG_DIR:   $GT_LOG_DIR"
echo ""

# ── Apply source patch ───────────────────────────────────────────────

echo "--- Applying source-level patch ---"

# Check if already patched
if grep -q "GT_HOOK_INJECT: initialize_runtime" "$RUN_INFER" 2>/dev/null; then
    echo "Patch already applied, skipping."
else
    python3 "$PATCHER" --source "$RUN_INFER" --in-place
    echo ""
    echo "Patch applied successfully."
fi

# Verify syntax of patched file
python3 -c "
import ast, sys
with open('$RUN_INFER') as f:
    source = f.read()
try:
    ast.parse(source)
    print('Syntax verification: OK')
except SyntaxError as e:
    print(f'SYNTAX ERROR: {e}')
    sys.exit(1)
"

if [ -n "${GT_DRY_RUN:-}" ]; then
    echo ""
    echo "DRY RUN: Patch applied. Not running evaluation."
    exit 0
fi

# ── Set up log directory ─────────────────────────────────────────────

mkdir -p "$GT_LOG_DIR"
export GT_HOOK_PATH
export GT_LOG_DIR

echo ""
echo "--- Starting evaluation ---"
echo "  Args: $*"
echo ""

# ── Run evaluation ───────────────────────────────────────────────────

cd "$OH_DIR"

# Use the OH venv if it exists
PYTHON="python3"
if [ -f "$OH_DIR/.venv/bin/python" ]; then
    PYTHON="$OH_DIR/.venv/bin/python"
fi

$PYTHON -m evaluation.benchmarks.swe_bench.run_infer "$@" 2>&1 | tee "$GT_LOG_DIR/run.log"
EXIT_CODE=${PIPESTATUS[0]}

# ── Post-run analysis ────────────────────────────────────────────────

echo ""
echo "================================================="
echo "  Run complete (exit code: $EXIT_CODE)"
echo "  $(date -u) UTC"
echo "================================================="

# Count hook logs
LOG_COUNT=0
if [ -d "$GT_LOG_DIR" ]; then
    LOG_COUNT=$(find "$GT_LOG_DIR" -name "*.jsonl" -not -name "run.log" | wc -l)
fi
echo "  Hook logs: $LOG_COUNT"

# Quick stats from JSONL logs
if [ "$LOG_COUNT" -gt 0 ]; then
    echo ""
    echo "--- Hook Log Summary ---"
    python3 -c "
import json, os, sys
log_dir = '$GT_LOG_DIR'
total_fires = 0
total_errors = 0
tasks_with_logs = 0
for fn in os.listdir(log_dir):
    if fn.endswith('.jsonl') and fn != 'run.log':
        tasks_with_logs += 1
        with open(os.path.join(log_dir, fn)) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    total_fires += 1
                    if entry.get('exit_code', 0) != 0:
                        total_errors += 1
                except json.JSONDecodeError:
                    pass
print(f'  Tasks with logs: {tasks_with_logs}')
print(f'  Total hook fires: {total_fires}')
print(f'  Hook errors: {total_errors}')
" 2>/dev/null || echo "  (could not parse hook logs)"
fi

# Run delivery gate if available
DELIVERY_GATE="$SCRIPT_DIR/oh_delivery_gate.py"
if [ -f "$DELIVERY_GATE" ] && [ "$LOG_COUNT" -gt 0 ]; then
    echo ""
    echo "--- Delivery Gate ---"
    python3 "$DELIVERY_GATE" scan --log-dir "$GT_LOG_DIR" 2>/dev/null || true
fi

exit $EXIT_CODE
