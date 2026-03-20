#!/usr/bin/env bash
# Run OpenHands SWE-bench inference with GroundTruth tools injected.
#
# Usage:
#   bash scripts/swebench/openhands_run_gt.sh <llm_config.json> [options]
#   bash scripts/swebench/openhands_run_gt.sh .llm_config/openai.json --workspace docker --max-iterations 300
#   bash scripts/swebench/openhands_run_gt.sh .llm_config/openai.json --select instances.txt --workspace docker
#
# This script:
# 1. Base64-encodes gt_tool.py
# 2. Creates env_setup_commands that decode it into /tmp/gt_tool.py inside the container
# 3. Runs OpenHands swebench-infer with the GT-enhanced prompt template
#
# For baseline (no GT): use swebench-infer directly with --prompt-path default.j2
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

# Copy GT prompt template to OpenHands prompts dir
GT_PROMPT_SRC="$REPO_DIR/benchmarks/swebench/prompts/gt_phase3.j2"
GT_PROMPT_DST="$OH_DIR/benchmarks/swebench/prompts/gt_phase3.j2"
if [ -f "$GT_PROMPT_SRC" ]; then
    cp "$GT_PROMPT_SRC" "$GT_PROMPT_DST"
    echo "Copied GT prompt template to $GT_PROMPT_DST"
else
    echo "ERROR: GT prompt template not found at $GT_PROMPT_SRC"
    exit 1
fi

# Base64-encode gt_tool.py for injection
GT_B64=$(base64 -w0 "$GT_TOOL")
echo "gt_tool.py encoded: ${#GT_B64} chars"

# Create a temporary Python script that injects gt_tool.py via env_setup_commands
# This patches the EvalMetadata to include the setup command
INJECT_SCRIPT=$(mktemp /tmp/gt_inject_XXXXXX.py)
cat > "$INJECT_SCRIPT" << 'PYEOF'
"""Monkey-patch OpenHands SWE-bench to inject gt_tool.py into containers."""
import sys
import os
import base64

# The base64-encoded gt_tool.py content (injected by shell script)
GT_B64 = os.environ["GT_TOOL_B64"]

# Decode command that will run inside the container
SETUP_CMD = f"echo '{GT_B64}' | base64 -d > /tmp/gt_tool.py && chmod +x /tmp/gt_tool.py && echo 'GT tool installed'"

# Monkey-patch: intercept EvalMetadata creation to add env_setup_commands
import benchmarks.swebench.run_infer as run_infer_mod
original_main = run_infer_mod.SWEBenchEvaluation.__init__

def patched_init(self, *args, **kwargs):
    original_main(self, *args, **kwargs)
    # Inject gt_tool.py setup command
    if self.metadata.env_setup_commands is None:
        self.metadata.env_setup_commands = []
    self.metadata.env_setup_commands.append(SETUP_CMD)
    print(f"[GT] Injected gt_tool.py setup command ({len(GT_B64)} bytes encoded)")

run_infer_mod.SWEBenchEvaluation.__init__ = patched_init

# Now run the actual main
if __name__ == "__main__":
    # Remove this script from argv so the parser sees the right args
    sys.argv = [sys.argv[0]] + sys.argv[1:]
    run_infer_mod.main()
PYEOF

echo "Inject script created at $INJECT_SCRIPT"

# Run with GT
cd "$OH_DIR"
source ~/.local/bin/env 2>/dev/null || true

export GT_TOOL_B64="$GT_B64"

echo ""
echo "=== Running OpenHands SWE-bench with GroundTruth ==="
echo "Args: $@"
echo "Prompt: gt_phase3.j2"
echo ""

uv run python "$INJECT_SCRIPT" "$@" --prompt-path gt_phase3.j2

# Cleanup
rm -f "$INJECT_SCRIPT"
