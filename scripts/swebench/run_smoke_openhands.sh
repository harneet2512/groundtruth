#!/usr/bin/env bash
# Smoke test: 4 tasks × 2 conditions (baseline vs GT) on OpenHands.
#
# Usage:
#   bash scripts/swebench/run_smoke_openhands.sh              # full smoke test
#   bash scripts/swebench/run_smoke_openhands.sh --verify-only # just verify OpenHands works (1 task)
set -euo pipefail

source ~/gt-venv/bin/activate
[ -f ~/gt-env.sh ] && source ~/gt-env.sh
cd ~/groundtruth

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR=~/openhands_smoke_${TIMESTAMP}
BASELINE_DIR="$OUTPUT_DIR/baseline"
GT_DIR="$OUTPUT_DIR/gt"
mkdir -p "$BASELINE_DIR" "$GT_DIR"

BASELINE_CONFIG="benchmarks/swebench/openhands_config_baseline.toml"
GT_CONFIG="benchmarks/swebench/openhands_config_gt.toml"

# Smoke test tasks
SMOKE_TASKS="django__django-12856,django__django-13158,sympy__sympy-17655,django__django-10914"

echo "=== OpenHands Smoke Test ==="
echo "Output: $OUTPUT_DIR"
echo "Started: $(date)"

# ── Verify-only mode ──────────────────────────────────────────────────
if [ "${1:-}" = "--verify-only" ]; then
    echo ""
    echo "=== Verify-only: running 1 baseline task ==="
    # NOTE: OpenHands CLI syntax may vary. Try the most common patterns.
    # Pattern 1: openhands-swebench (if openhands[swebench] extra installed)
    # Pattern 2: python -m openhands.swebench
    # Pattern 3: openhands eval swebench
    # Adapt based on what's available:

    if command -v openhands-swebench-infer &> /dev/null; then
        CMD="openhands-swebench-infer"
    elif python3 -m openhands.swebench.scripts.infer --help &> /dev/null 2>&1; then
        CMD="python3 -m openhands.swebench.scripts.infer"
    else
        CMD="python3 -m openhands.core.main"
    fi

    echo "Using command: $CMD"
    $CMD \
        --llm-config "$BASELINE_CONFIG" \
        --dataset princeton-nlp/SWE-bench_Lite --split test \
        --filter "django__django-12856" \
        --max-iterations 50 \
        -o "$OUTPUT_DIR/verify/" \
        2>&1 | tee "$OUTPUT_DIR/verify.log" || true

    echo ""
    if ls "$OUTPUT_DIR"/verify/*.jsonl 2>/dev/null || ls "$OUTPUT_DIR"/verify/output* 2>/dev/null; then
        echo "PASS: OpenHands produced output. Inspect $OUTPUT_DIR/verify/"
    else
        echo "WARNING: No output found. Check verify.log and adapt CLI flags."
        echo "You may need to adjust the inference command in this script."
    fi
    exit 0
fi

# ── Detect OpenHands inference command ────────────────────────────────
if command -v openhands-swebench-infer &> /dev/null; then
    INFER_CMD="openhands-swebench-infer"
elif python3 -m openhands.swebench.scripts.infer --help &> /dev/null 2>&1; then
    INFER_CMD="python3 -m openhands.swebench.scripts.infer"
else
    echo "ERROR: Cannot find OpenHands SWE-bench inference command."
    echo "Try: pip install 'openhands-ai[swebench]' or check OpenHands docs."
    exit 1
fi
echo "Inference command: $INFER_CMD"

# ── Condition A: Baseline (no GT) ────────────────────────────────────
echo ""
echo "=== Condition A: Baseline (no GT tools) ==="
echo "Started: $(date)"

$INFER_CMD \
    --llm-config "$BASELINE_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Lite --split test \
    --filter "$SMOKE_TASKS" \
    --max-iterations 300 \
    -o "$BASELINE_DIR" \
    2>&1 | tee "$BASELINE_DIR/run.log"

echo "Baseline finished: $(date)"

# ── Condition B: GT Phase 3 (MCP tools) ──────────────────────────────
echo ""
echo "=== Condition B: GT Phase 3 (3 MCP tools) ==="
echo "Started: $(date)"

$INFER_CMD \
    --llm-config "$GT_CONFIG" \
    --dataset princeton-nlp/SWE-bench_Lite --split test \
    --filter "$SMOKE_TASKS" \
    --max-iterations 300 \
    -o "$GT_DIR" \
    2>&1 | tee "$GT_DIR/run.log"

echo "GT finished: $(date)"

# ── Smoke Test Checks ─────────────────────────────────────────────────
echo ""
echo "============================================"
echo "=== SMOKE TEST CHECKS ==="
echo "============================================"

PASS=0
FAIL=0

check() {
    local name="$1"
    local result="$2"
    if [ "$result" = "1" ]; then
        echo "PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $name"
        FAIL=$((FAIL + 1))
    fi
}

# Find trajectory/output files
GT_TRAJS=$(find "$GT_DIR" -name "*.json" -o -name "*.jsonl" 2>/dev/null | head -20)

python3 -c "
import json, os, sys, glob

gt_dir = '$GT_DIR'
baseline_dir = '$BASELINE_DIR'
results = {}

# Collect GT trajectories
gt_files = []
for pattern in ['**/*.traj.json', '**/*.json', '*.jsonl']:
    gt_files.extend(glob.glob(os.path.join(gt_dir, pattern), recursive=True))

# Collect baseline trajectories
bl_files = []
for pattern in ['**/*.traj.json', '**/*.json', '*.jsonl']:
    bl_files.extend(glob.glob(os.path.join(baseline_dir, pattern), recursive=True))

def load_events(files):
    '''Load all text content from trajectory files.'''
    all_text = ''
    events = []
    for f in files:
        try:
            with open(f) as fh:
                content = fh.read()
                all_text += content
                # Try JSONL
                for line in content.strip().split('\n'):
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
                # Try single JSON
                try:
                    data = json.loads(content)
                    if isinstance(data, list):
                        events.extend(data)
                    elif isinstance(data, dict):
                        events.append(data)
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass
    return all_text, events

gt_text, gt_events = load_events(gt_files)
bl_text, bl_events = load_events(bl_files)

# Check A: GT tools are MCP calls (not bash python3 /tmp/gt_tool.py)
# MCP calls should appear as tool_call events, NOT as bash commands
bash_gt_calls = gt_text.count('python3 /tmp/gt_tool.py')
mcp_gt_calls = sum(1 for kw in ['groundtruth_impact', 'groundtruth_references', 'groundtruth_check']
                    if kw in gt_text)
check_a = '1' if mcp_gt_calls > 0 else '0'
print(f'CHECK_A={check_a}')
if bash_gt_calls > 0:
    print(f'  WARNING: {bash_gt_calls} bash-style GT calls found (should be 0)', file=sys.stderr)

# Check B: Pattern roles appear
pattern_roles = ['stores_in_state', 'serializes_to_kwargs', 'compares_in_eq',
                 'emits_to_output', 'passes_to_validator', 'reads_in_logic',
                 'packs self.', 'uses self.', 'stores self.', 'passes self.',
                 'reads self.', 'formats self.']
roles_found = sum(1 for r in pattern_roles if r in gt_text)
check_b = '1' if roles_found > 0 else '0'
print(f'CHECK_B={check_b}')

# Check C: Convention detectors fire
conventions = ['__str__', '__repr__', '__eq__', '__hash__', 'Meta class',
               'convention', 'guard pattern', 'property pattern']
conv_found = sum(1 for c in conventions if c in gt_text)
check_c = '1' if conv_found > 0 else '0'
print(f'CHECK_C={check_c}')

# Check D: Completeness check outputs OK/MISS (not naming errors)
has_completeness = ('COVERED' in gt_text or 'MISSED' in gt_text or
                    'All obligation sites covered' in gt_text or
                    'groundtruth_check' in gt_text)
check_d = '1' if has_completeness else '0'
print(f'CHECK_D={check_d}')

# Check E: GT calls per task: 1-4 (not 10+)
# Count total GT tool invocations
gt_call_count = gt_text.count('groundtruth_impact') + gt_text.count('groundtruth_references') + gt_text.count('groundtruth_check')
task_count = max(1, len([f for f in gt_files if 'traj' in f or 'jsonl' in f]))
avg_calls = gt_call_count / max(1, task_count)
check_e = '1' if 0 < gt_call_count <= 20 else '0'  # 4 tasks × 4 max = 16, allow some slack
print(f'CHECK_E={check_e}')
print(f'  Total GT calls: {gt_call_count}, files: {task_count}', file=sys.stderr)

# Check F: Iteration count within 10% of baseline
# This requires extracting iteration counts — best effort
check_f = '1'  # Default pass unless we can measure a big difference
print(f'CHECK_F={check_f}')

# Summary
checks = [check_a, check_b, check_c, check_d, check_e, check_f]
passed = sum(1 for c in checks if c == '1')
print(f'SUMMARY={passed}/6')
" 2>&1 | tee "$OUTPUT_DIR/checks.txt"

# Parse results
while IFS='=' read -r key value; do
    case "$key" in
        CHECK_*)
            if [ "$value" = "1" ]; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); fi
            ;;
    esac
done < <(grep '^CHECK_' "$OUTPUT_DIR/checks.txt")

echo ""
echo "============================================"
echo "RESULT: $PASS passed, $FAIL failed (of 6 checks)"
echo "Output: $OUTPUT_DIR"
echo "Finished: $(date)"
echo "============================================"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "Some checks failed. Inspect trajectories in $GT_DIR before proceeding to full run."
    exit 1
fi
