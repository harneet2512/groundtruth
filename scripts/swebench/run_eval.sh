#!/usr/bin/env bash
# run_eval.sh — Durable SWE-bench Docker evaluation wrapper.
#
# Sets Docker timeout, cleans stale containers, applies the timeout monkey-patch
# idempotently, then runs swebench.harness.run_evaluation.
#
# Usage: bash scripts/swebench/run_eval.sh <predictions_path> <run_id> [max_workers]

set -euo pipefail

PREDICTIONS_PATH="${1:?Usage: run_eval.sh <predictions_path> <run_id> [max_workers]}"
RUN_ID="${2:?Usage: run_eval.sh <predictions_path> <run_id> [max_workers]}"
MAX_WORKERS="${3:-2}"

export DOCKER_CLIENT_TIMEOUT=600
export COMPOSE_HTTP_TIMEOUT=600

echo "=== SWE-bench Eval Wrapper ==="
echo "Predictions: $PREDICTIONS_PATH"
echo "Run ID:      $RUN_ID"
echo "Workers:     $MAX_WORKERS"
echo "Docker timeout: $DOCKER_CLIENT_TIMEOUT"

# --- Clean stale containers ---
echo "Cleaning stale sweb.eval containers..."
stale=$(docker ps -aq --filter "name=sweb.eval" 2>/dev/null || true)
if [ -n "$stale" ]; then
    docker rm -f $stale 2>/dev/null || true
    echo "Removed stale containers."
else
    echo "No stale containers found."
fi

# --- Apply Docker timeout patch idempotently ---
HARNESS_FILE=$(python3 -c "import swebench.harness.run_evaluation as m; print(m.__file__)" 2>/dev/null || true)
if [ -n "$HARNESS_FILE" ]; then
    if grep -q 'docker.from_env()' "$HARNESS_FILE"; then
        echo "Patching docker.from_env() -> docker.from_env(timeout=600) in $HARNESS_FILE"
        sed -i 's/docker\.from_env()/docker.from_env(timeout=600)/g' "$HARNESS_FILE"
        echo "Patch applied."
    else
        echo "Docker timeout patch already applied (or pattern not found)."
    fi
else
    echo "WARNING: Could not locate swebench harness file. Skipping patch."
fi

# --- Run evaluation ---
echo "Starting evaluation..."
python3 -m swebench.harness.run_evaluation \
    --predictions_path "$PREDICTIONS_PATH" \
    --run_id "$RUN_ID" \
    --max_workers "$MAX_WORKERS" \
    --cache_level env

echo "=== Evaluation complete ==="
