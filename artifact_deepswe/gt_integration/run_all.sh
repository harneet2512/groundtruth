#!/bin/bash
# Batch runner for DeepSWE tasks with GroundTruth injection.
#
# Usage:
#   ./run_all.sh [--smoke N] [--language LANG] [--workers W] [--model MODEL]
#
# Examples:
#   ./run_all.sh --smoke 5                          # 5 random tasks
#   ./run_all.sh --language python --workers 2      # All 34 Python tasks, 2 parallel
#   ./run_all.sh --language go --smoke 10            # 10 random Go tasks
#   ./run_all.sh                                     # All 113 tasks, 1 worker

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST="$REPO_ROOT/repo_manifest.json"

# Defaults
SMOKE=0
LANGUAGE=""
WORKERS=1
MODEL="${MODEL:-deepseek/deepseek-v4-flash}"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO_ROOT/results/batch_$(date +%Y%m%dT%H%M%S)}"
INDEXES_ROOT="${GT_PREBUILT_INDEXES_ROOT:-}"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --smoke) SMOKE="$2"; shift 2 ;;
        --language) LANGUAGE="$2"; shift 2 ;;
        --workers|-w) WORKERS="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --output-dir|-o) OUTPUT_DIR="$2"; shift 2 ;;
        --indexes-root) INDEXES_ROOT="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--smoke N] [--language LANG] [--workers W] [--model MODEL]"
            echo "  --smoke N       Run only N random tasks"
            echo "  --language LANG Filter by language (python|go|typescript|javascript|rust)"
            echo "  --workers W     Parallel workers (default: 1)"
            echo "  --model MODEL   LLM model (default: deepseek/deepseek-v4-flash)"
            echo "  --output-dir D  Results directory"
            echo "  --indexes-root  Pre-built graph.db directory"
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ ! -f "$MANIFEST" ]]; then
    echo "ERROR: repo_manifest.json not found at $MANIFEST"
    echo "Run scripts/build_manifest.py first"
    exit 1
fi

# Extract task list from manifest
echo "Reading manifest: $MANIFEST"

TASK_IDS=$(python3 -c "
import json, random, sys
with open('$MANIFEST') as f:
    m = json.load(f)
tasks = m['tasks']
lang = '$LANGUAGE'
if lang:
    tasks = [t for t in tasks if t['language'] == lang]
smoke = $SMOKE
if smoke > 0:
    random.seed(42)
    tasks = random.sample(tasks, min(smoke, len(tasks)))
for t in tasks:
    print(t['instance_id'])
")

TOTAL=$(echo "$TASK_IDS" | wc -l | tr -d ' ')

echo ""
echo "=== DeepSWE + GroundTruth Batch Run ==="
echo "Tasks:    $TOTAL"
echo "Language: ${LANGUAGE:-all}"
echo "Smoke:    ${SMOKE:-off}"
echo "Workers:  $WORKERS"
echo "Model:    $MODEL"
echo "Output:   $OUTPUT_DIR"
echo "Indexes:  ${INDEXES_ROOT:-none}"
echo ""

mkdir -p "$OUTPUT_DIR"

# Save run config
cat > "$OUTPUT_DIR/run_config.json" << REOF
{
  "total_tasks": $TOTAL,
  "language_filter": "${LANGUAGE:-all}",
  "smoke": $SMOKE,
  "workers": $WORKERS,
  "model": "$MODEL",
  "indexes_root": "$INDEXES_ROOT",
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
REOF

# Export for sub-processes
export GT_PREBUILT_INDEXES_ROOT="$INDEXES_ROOT"
export MODEL

# Run tasks
COMPLETED=0
FAILED=0

run_task() {
    local task_id="$1"
    local task_output="$OUTPUT_DIR"

    echo "[$(date +%H:%M:%S)] Starting: $task_id"

    if "$SCRIPT_DIR/run_deepswe.sh" "$task_id" \
        --model "$MODEL" \
        --output-dir "$task_output" \
        --indexes-root "$INDEXES_ROOT" \
        > "$task_output/logs/${task_id}.log" 2>&1; then
        echo "[$(date +%H:%M:%S)] DONE: $task_id"
    else
        echo "[$(date +%H:%M:%S)] FAIL: $task_id"
    fi
}

export -f run_task
export SCRIPT_DIR OUTPUT_DIR MODEL INDEXES_ROOT

mkdir -p "$OUTPUT_DIR/logs"

if [[ $WORKERS -gt 1 ]]; then
    echo "Running $TOTAL tasks with $WORKERS parallel workers..."
    echo "$TASK_IDS" | xargs -P "$WORKERS" -I {} bash -c 'run_task "$@"' _ {}
else
    echo "Running $TOTAL tasks sequentially..."
    while IFS= read -r task_id; do
        run_task "$task_id"
    done <<< "$TASK_IDS"
fi

echo ""
echo "=== Batch Run Complete ==="
echo "Output: $OUTPUT_DIR"
echo "Total tasks: $TOTAL"

# Count results
if [[ -d "$OUTPUT_DIR/gt_meta" ]]; then
    META_COUNT=$(ls -1 "$OUTPUT_DIR/gt_meta/"*.json 2>/dev/null | wc -l | tr -d ' ')
    BRIEF_COUNT=$(grep -l '"brief_generated": true' "$OUTPUT_DIR/gt_meta/"*.json 2>/dev/null | wc -l | tr -d ' ')
    echo "GT metadata files: $META_COUNT"
    echo "Briefs generated: $BRIEF_COUNT"
fi
