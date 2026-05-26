#!/bin/bash
# Run a single DeepSWE task with GroundTruth injection.
#
# Usage:
#   ./run_deepswe.sh <task_id> [--model MODEL] [--output-dir DIR] [--indexes-root DIR]
#
# Environment:
#   DEEPSEEK_API_KEY        — Required for DeepSeek V4 Flash
#   GT_PREBUILT_INDEXES_ROOT — Directory with {task_id}/graph.db (auto-set from --indexes-root)
#   GT_INDEX_BINARY          — Path to gt-index-linux for fallback in-container indexing
#
# Examples:
#   ./run_deepswe.sh dateutil-rfc5545-timezone-interop
#   ./run_deepswe.sh expr-try-catch-errors --model openai/gpt-4o --output-dir ~/results
#   ./run_deepswe.sh kombu-single-active-consumer-priority --indexes-root /data/indexes

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Defaults
MODEL="${MODEL:-deepseek/deepseek-v4-flash}"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO_ROOT/results}"
INDEXES_ROOT="${GT_PREBUILT_INDEXES_ROOT:-}"
TASK_ID=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --model) MODEL="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --indexes-root) INDEXES_ROOT="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 <task_id> [--model MODEL] [--output-dir DIR] [--indexes-root DIR]"
            exit 0 ;;
        -*) echo "Unknown option: $1"; exit 1 ;;
        *) TASK_ID="$1"; shift ;;
    esac
done

if [[ -z "$TASK_ID" ]]; then
    echo "ERROR: task_id is required"
    echo "Usage: $0 <task_id> [--model MODEL] [--output-dir DIR]"
    exit 1
fi

# Validate task exists
TASK_DIR="$REPO_ROOT/../deepswe-bench/tasks/$TASK_ID"
if [[ ! -d "$TASK_DIR" ]]; then
    TASK_DIR="$REPO_ROOT/deepswe-bench/tasks/$TASK_ID"
fi
if [[ ! -d "$TASK_DIR" ]]; then
    echo "ERROR: Task directory not found for $TASK_ID"
    echo "Looked in: $REPO_ROOT/../deepswe-bench/tasks/$TASK_ID"
    exit 1
fi

echo "=== DeepSWE + GroundTruth ==="
echo "Task:    $TASK_ID"
echo "Model:   $MODEL"
echo "Output:  $OUTPUT_DIR"
echo "Indexes: ${INDEXES_ROOT:-'(none — will index in-container if GT_INDEX_BINARY set)'}"
echo ""

# Set environment
export GT_PREBUILT_INDEXES_ROOT="$INDEXES_ROOT"
export MODEL

# Find gt-index-linux binary for fallback
if [[ -z "${GT_INDEX_BINARY:-}" ]]; then
    for candidate in \
        "$SCRIPT_DIR/../../gt-index/gt-index-linux" \
        "$REPO_ROOT/../gt-index/gt-index-linux" \
        "/usr/local/bin/gt-index" \
        "$HOME/gt-index-linux"; do
        if [[ -f "$candidate" ]]; then
            export GT_INDEX_BINARY="$candidate"
            echo "Found gt-index-linux: $GT_INDEX_BINARY"
            break
        fi
    done
fi

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Check if pre-built index exists
if [[ -n "$INDEXES_ROOT" && -f "$INDEXES_ROOT/$TASK_ID/graph.db" ]]; then
    DB_SIZE=$(stat -f%z "$INDEXES_ROOT/$TASK_ID/graph.db" 2>/dev/null || stat -c%s "$INDEXES_ROOT/$TASK_ID/graph.db" 2>/dev/null || echo "?")
    echo "Pre-built index: $INDEXES_ROOT/$TASK_ID/graph.db ($DB_SIZE bytes)"
else
    echo "No pre-built index for $TASK_ID"
    if [[ -n "${GT_INDEX_BINARY:-}" ]]; then
        echo "Will index in-container using $GT_INDEX_BINARY"
    else
        echo "WARNING: No index and no gt-index binary — GT brief will be empty"
    fi
fi

echo ""
echo "Starting agent..."
echo "---"

# Run the patched mini-swe-agent
python3 "$SCRIPT_DIR/patch_mini_swe.py" swebench \
    -c "$SCRIPT_DIR/deepswe_gt.yaml" \
    --model "$MODEL" \
    --instance-id "$TASK_ID" \
    -o "$OUTPUT_DIR" \
    2>&1

echo "---"
echo "Done. Results in $OUTPUT_DIR"

# Print GT metadata if available
META_FILE="$OUTPUT_DIR/gt_meta/$TASK_ID.json"
if [[ -f "$META_FILE" ]]; then
    echo ""
    echo "GT Metadata:"
    cat "$META_FILE"
fi
