#!/usr/bin/env bash
# Run OpenHands SWE-bench inference with GroundTruth tools + Vertex AI Qwen3-Coder.
#
# Usage:
#   bash scripts/swebench/openhands_run_vertex_gt.sh [options]
#   bash scripts/swebench/openhands_run_vertex_gt.sh --instances "django__django-12856,django__django-14608"
#   bash scripts/swebench/openhands_run_vertex_gt.sh --output-dir results/gt --max-iterations 300
#
# This script:
# 1. Base64-encodes gt_tool.py
# 2. Creates env_setup_commands that decode it into /tmp/gt_tool.py inside the container
# 3. Runs OpenHands swebench-infer with the GT-enhanced prompt template + MCP bridge
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="${OH_DIR:-$HOME/oh-benchmarks}"

# Verify gt_tool.py exists
GT_TOOL="$REPO_DIR/benchmarks/swebench/gt_tool.py"
if [ ! -f "$GT_TOOL" ]; then
    echo "ERROR: gt_tool.py not found at $GT_TOOL"
    exit 1
fi

# Copy GT prompt template
GT_PROMPT_SRC="$REPO_DIR/benchmarks/swebench/prompts/gt_phase3.j2"
GT_PROMPT_DST="$OH_DIR/benchmarks/swebench/prompts/gt_phase3.j2"
if [ -f "$GT_PROMPT_SRC" ]; then
    cp "$GT_PROMPT_SRC" "$GT_PROMPT_DST"
    echo "Copied GT prompt template to $GT_PROMPT_DST"
else
    echo "ERROR: GT prompt template not found at $GT_PROMPT_SRC"
    exit 1
fi

# Copy and configure GT MCP bridge config
GT_BRIDGE="$REPO_DIR/benchmarks/swebench/gt_mcp_bridge.py"
GT_CONFIG_SRC="$REPO_DIR/benchmarks/swebench/openhands_config_gt.toml"
GT_CONFIG_DST="$OH_DIR/openhands_config_gt.toml"
cp "$GT_CONFIG_SRC" "$GT_CONFIG_DST"
# Replace BRIDGE_PATH_PLACEHOLDER with actual path
sed -i "s|BRIDGE_PATH_PLACEHOLDER|$GT_BRIDGE|g" "$GT_CONFIG_DST"
echo "GT config: $GT_CONFIG_DST (bridge → $GT_BRIDGE)"

# Stage gt_tool.py for injection via env_setup_commands
# Split base64 into chunks to avoid shell arg length limits
GT_B64_FILE=$(mktemp /tmp/gt_b64_XXXXXX.txt)
base64 -w0 "$GT_TOOL" > "$GT_B64_FILE"
echo "gt_tool.py encoded: $(wc -c < "$GT_B64_FILE") bytes → $GT_B64_FILE"

# Create inject script that uses chunked file copy
INJECT_SCRIPT=$(mktemp /tmp/gt_inject_XXXXXX.py)
cat > "$INJECT_SCRIPT" << PYEOF
"""Monkey-patch OpenHands SWE-bench to inject gt_tool.py into containers.

Uses chunked base64 to avoid shell arg length limits.
"""
import sys
import os
import textwrap

# Read base64 from file
with open("$GT_B64_FILE") as f:
    gt_b64 = f.read().strip()

# Split into 50KB chunks for shell safety
CHUNK_SIZE = 50000
chunks = textwrap.wrap(gt_b64, CHUNK_SIZE)

# Build setup commands: write chunks to temp file, then decode
setup_cmds = [f"rm -f /tmp/gt_b64.txt && touch /tmp/gt_b64.txt"]
for chunk in chunks:
    setup_cmds.append(f"echo -n '{chunk}' >> /tmp/gt_b64.txt")
setup_cmds.append("base64 -d /tmp/gt_b64.txt > /tmp/gt_tool.py && chmod +x /tmp/gt_tool.py && rm /tmp/gt_b64.txt && echo 'GT tool installed'")

import benchmarks.swebench.run_infer as run_infer_mod
original_init = run_infer_mod.SWEBenchEvaluation.__init__

def patched_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    if self.metadata.env_setup_commands is None:
        self.metadata.env_setup_commands = []
    self.metadata.env_setup_commands.extend(setup_cmds)
    print(f"[GT] Injected gt_tool.py via {len(setup_cmds)} setup commands ({len(gt_b64)} bytes)")

run_infer_mod.SWEBenchEvaluation.__init__ = patched_init

if __name__ == "__main__":
    sys.argv = [sys.argv[0]] + sys.argv[1:]
    run_infer_mod.main()
PYEOF

echo "Inject script created at $INJECT_SCRIPT"

# Parse arguments
EXTRA_ARGS=()
OUTPUT_DIR=""
INSTANCES=""
MAX_ITER="100"

while [[ $# -gt 0 ]]; do
    case $1 in
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --instances) INSTANCES="$2"; shift 2 ;;
        --max-iterations) MAX_ITER="$2"; shift 2 ;;
        *) EXTRA_ARGS+=("$1"); shift ;;
    esac
done

cd "$OH_DIR"
source ~/.local/bin/env 2>/dev/null || true

echo ""
echo "=== Running OpenHands SWE-bench with GroundTruth (Vertex AI Qwen3-Coder) ==="
echo "Prompt: gt_phase3.j2"
echo "Max iterations: $MAX_ITER"
[ -n "$INSTANCES" ] && echo "Instances: $INSTANCES"
[ -n "$OUTPUT_DIR" ] && echo "Output: $OUTPUT_DIR"
echo ""

CMD=(uv run python "$INJECT_SCRIPT"
    .llm_config/vertex_qwen3.json
    --dataset princeton-nlp/SWE-bench_Lite
    --split test
    --max-iterations "$MAX_ITER"
    --prompt-path gt_phase3.j2
    --workspace docker
    --n-critic-runs 1
    --max-retries 1
)

[ -n "$OUTPUT_DIR" ] && CMD+=(--output-dir "$OUTPUT_DIR")

# Handle --instances: file path or comma-separated list
if [ -n "$INSTANCES" ]; then
    if [ -f "$INSTANCES" ]; then
        CMD+=(--select "$INSTANCES")
    else
        SELECT_FILE=$(mktemp /tmp/gt_select_XXXXXX.txt)
        echo "$INSTANCES" | tr ',' '\n' > "$SELECT_FILE"
        CMD+=(--select "$SELECT_FILE")
    fi
fi

[ ${#EXTRA_ARGS[@]} -gt 0 ] && CMD+=("${EXTRA_ARGS[@]}")

"${CMD[@]}"

# Cleanup
rm -f "$INJECT_SCRIPT" "$GT_B64_FILE"
[ -n "${SELECT_FILE:-}" ] && rm -f "$SELECT_FILE"
