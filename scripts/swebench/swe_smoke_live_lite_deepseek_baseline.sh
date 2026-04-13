#!/bin/bash
set -euo pipefail

# 10-task smoke for the canonical stage-1 baseline.
# Use this before any broader DeepSeek or GT run.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SELECT_FILE="${SELECT_FILE:-/tmp/swe_live_lite_smoke_10.txt}"
NUM_WORKERS="${NUM_WORKERS:-4}"
OUTPUT_DIR="${OUTPUT_DIR:-$HOME/results/sweagent_deepseek_v32_live_lite_smoke}"

if [ ! -f "$SELECT_FILE" ]; then
    cat > "$SELECT_FILE" <<EOF
astropy__astropy-12907
astropy__astropy-13033
astropy__astropy-13236
astropy__astropy-13398
astropy__astropy-13453
astropy__astropy-13579
astropy__astropy-13977
astropy__astropy-14096
astropy__astropy-14182
astropy__astropy-14309
EOF
fi

echo "=== SWE-agent + DeepSeek V3.2 BASELINE SMOKE ==="
echo "Select file: $SELECT_FILE"
echo "Workers: $NUM_WORKERS"
echo "Output: $OUTPUT_DIR"
echo ""

bash "$SCRIPT_DIR/swe_run_live_lite_deepseek_baseline.sh" \
  --instances.filter_path "$SELECT_FILE" \
  --num_workers "$NUM_WORKERS" \
  --output_dir "$OUTPUT_DIR" \
  "$@"

echo ""
echo "Expected review after smoke:"
echo "  - patch rate"
echo "  - zero-edit rate"
echo "  - median turns before first edit"
echo "  - empty patch rate"
echo "  - infra failure count"
echo "  - token usage and cost band"
