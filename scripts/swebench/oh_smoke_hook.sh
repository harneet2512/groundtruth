#!/bin/bash
set -euo pipefail

# Smoke test for gt_hook.py v7 constraint map hook.
# Runs N Django tasks with the hook injected + v7 prompt template.
# Gate: hook must fire (non-empty stdout) on >=3 tasks with no crashes.
#
# Usage:
#   bash oh_smoke_hook.sh [--num-workers 2] [--instances django1,django2,...] [--num-tasks 10|50]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"
LLM_CONFIG="$OH_DIR/.llm_config/vertex_qwen3.json"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="$HOME/results/smoke_hook_${TIMESTAMP}"
GT_LOG_DIR="$OUTPUT_DIR/gt_logs"
NUM_WORKERS=2
NUM_TASKS=0
PROMPT_TEMPLATE="gt_hook_v7.j2"

# 10 Django instances for smoke test (default)
DEFAULT_INSTANCES="django__django-10097,django__django-10554,django__django-10880,django__django-10914,django__django-10973,django__django-11066,django__django-11087,django__django-11095,django__django-11099,django__django-11133"

INSTANCES=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --num-workers) NUM_WORKERS="$2"; shift 2 ;;
        --instances)   INSTANCES="$2";  shift 2 ;;
        --num-tasks)   NUM_TASKS="$2";  shift 2 ;;
        --prompt)      PROMPT_TEMPLATE="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# If --num-tasks is set and --instances is not, auto-select from instances_a.txt
if [ "$NUM_TASKS" -gt 0 ] && [ -z "$INSTANCES" ]; then
    INSTANCES_FILE="$SCRIPT_DIR/instances_a.txt"
    if [ ! -f "$INSTANCES_FILE" ]; then
        echo "ERROR: instances_a.txt not found at $INSTANCES_FILE"
        exit 1
    fi
    INSTANCES=$(head -n "$NUM_TASKS" "$INSTANCES_FILE" | paste -sd, -)
    echo "Auto-selected $NUM_TASKS tasks from instances_a.txt"
fi

# Fall back to default 10 instances
if [ -z "$INSTANCES" ]; then
    INSTANCES="$DEFAULT_INSTANCES"
fi

# Check proxy
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running on port 4000."
    echo "Start it: bash $SCRIPT_DIR/oh_setup_proxy.sh"
    exit 1
fi
echo "Proxy: OK"

# Check gt_hook.py exists
GT_HOOK="$REPO_DIR/benchmarks/swebench/gt_hook.py"
if [ ! -f "$GT_HOOK" ]; then
    echo "ERROR: gt_hook.py not found at $GT_HOOK"
    exit 1
fi
echo "gt_hook.py: $(wc -c < "$GT_HOOK") bytes"

# Copy prompt template to OH prompts dir
PROMPT_SRC="$SCRIPT_DIR/prompts/$PROMPT_TEMPLATE"
PROMPT_DEST="$OH_DIR/benchmarks/swebench/prompts/$PROMPT_TEMPLATE"
if [ -f "$PROMPT_SRC" ]; then
    mkdir -p "$(dirname "$PROMPT_DEST")"
    cp "$PROMPT_SRC" "$PROMPT_DEST"
    echo "Prompt template: $PROMPT_TEMPLATE -> $PROMPT_DEST"
else
    echo "WARNING: Prompt template not found at $PROMPT_SRC, running without custom prompt"
    PROMPT_TEMPLATE=""
fi

TASK_COUNT=$(echo "$INSTANCES" | tr ',' '\n' | wc -l)
SMOKE_GATE=$(( TASK_COUNT * 3 / 10 ))  # 30% fire rate gate
[ "$SMOKE_GATE" -lt 2 ] && SMOKE_GATE=2

mkdir -p "$OUTPUT_DIR" "$GT_LOG_DIR"
export GT_LOG_DIR

echo ""
echo "================================================="
echo "  GT v7 Hook Smoke Test"
echo "  Started:  $(date -u) UTC"
echo "  Output:   $OUTPUT_DIR"
echo "  Logs:     $GT_LOG_DIR"
echo "  Workers:  $NUM_WORKERS"
echo "  Tasks:    $TASK_COUNT"
echo "  Prompt:   $PROMPT_TEMPLATE"
echo "  Gate:     >= $SMOKE_GATE tasks fire"
echo "================================================="
echo ""

# Build command
CMD_ARGS=(
    "$SCRIPT_DIR/oh_gt_hook_wrapper.py" "$LLM_CONFIG"
    --dataset princeton-nlp/SWE-bench_Lite
    --split test
    --workspace docker
    --max-iterations 50
    --num-workers "$NUM_WORKERS"
    --filter-instances "$INSTANCES"
    --output-dir "$OUTPUT_DIR"
)

# Add prompt path if available
if [ -n "$PROMPT_TEMPLATE" ]; then
    CMD_ARGS+=(--prompt-path "$PROMPT_TEMPLATE")
fi

cd "$OH_DIR"
uv run python "${CMD_ARGS[@]}" 2>&1 | tee "$OUTPUT_DIR/run.log"

echo ""
echo "================================================="
echo "  Smoke test run complete: $(date -u) UTC"
echo "================================================="
echo ""

# Analyze hook logs
if [ -d "$GT_LOG_DIR" ] && [ "$(ls -A "$GT_LOG_DIR" 2>/dev/null)" ]; then
    echo "Analyzing hook logs..."
    python3 "$SCRIPT_DIR/analyze_hook_logs.py" "$GT_LOG_DIR" --smoke-gate "$SMOKE_GATE"

    # V7-specific analysis: check cross-file intelligence
    echo ""
    echo "=== V7 Cross-File Intelligence Analysis ==="
    python3 -c "
import json, os, sys
from collections import Counter

log_dir = '$GT_LOG_DIR'
tasks_with_crossfile = 0
tasks_with_tests = 0
tasks_with_norms = 0
total_tasks = 0

for fname in sorted(os.listdir(log_dir)):
    if not fname.endswith('.jsonl'):
        continue
    task_id = fname.replace('.jsonl', '')
    total_tasks += 1
    has_crossfile = False
    has_tests = False
    has_norms = False

    with open(os.path.join(log_dir, fname)) as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if entry.get('endpoint') != 'understand':
                continue
            wacga = entry.get('what_agent_cannot_get_alone', {})
            if wacga.get('cross_file_callers'):
                has_crossfile = True
            if wacga.get('test_file_discovery'):
                has_tests = True
            if wacga.get('mined_norms'):
                has_norms = True

    if has_crossfile: tasks_with_crossfile += 1
    if has_tests: tasks_with_tests += 1
    if has_norms: tasks_with_norms += 1

print(f'Total tasks with understand logs: {total_tasks}')
print(f'  Cross-file callers present: {tasks_with_crossfile}/{total_tasks}')
print(f'  Test file discovery:        {tasks_with_tests}/{total_tasks}')
print(f'  Mined norms:                {tasks_with_norms}/{total_tasks}')
print()
novel = tasks_with_crossfile + tasks_with_tests + tasks_with_norms
total_possible = total_tasks * 3
if total_possible > 0:
    print(f'Novel intelligence rate: {novel}/{total_possible} ({100*novel/total_possible:.0f}%)')
"
else
    echo "WARNING: No hook logs found in $GT_LOG_DIR"
    echo "         Hook may not have fired or log extraction failed."
fi
