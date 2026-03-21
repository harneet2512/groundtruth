#!/usr/bin/env bash
# Run OpenHands SWE-bench inference with GroundTruth pre-computed analysis + Vertex AI Qwen3-Coder.
#
# Usage:
#   bash scripts/swebench/openhands_run_vertex_gt.sh [options]
#   bash scripts/swebench/openhands_run_vertex_gt.sh --instances "django__django-12856,django__django-14608"
#   bash scripts/swebench/openhands_run_vertex_gt.sh --output-dir results/gt --max-iterations 100
#
# This script:
# 1. Base64-encodes gt_tool.py for container injection
# 2. Extracts symbols from each task's problem_statement
# 3. Monkey-patches OpenHands to:
#    a) Inject gt_tool.py into containers via env_setup_commands
#    b) Run GT analysis per-instance inside the container (env_setup)
#    c) Inject pre-computed analysis into the prompt template context
# 4. Runs OpenHands swebench-infer with the GT-enhanced prompt template
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

# Stage gt_tool.py for injection via env_setup_commands
# Split base64 into chunks to avoid shell arg length limits
GT_B64_FILE=$(mktemp /tmp/gt_b64_XXXXXX.txt)
base64 -w0 "$GT_TOOL" > "$GT_B64_FILE"
echo "gt_tool.py encoded: $(wc -c < "$GT_B64_FILE") bytes → $GT_B64_FILE"

# Create inject script with pre-computed analysis support
INJECT_SCRIPT=$(mktemp /tmp/gt_inject_XXXXXX.py)
cat > "$INJECT_SCRIPT" << 'PYEOF'
"""Monkey-patch OpenHands SWE-bench to inject gt_tool.py + pre-computed analysis.

Two injection mechanisms:
1. gt_tool.py is installed into containers via env_setup_commands (chunked base64)
2. Per-instance GT analysis is run inside the container during setup and saved to
   /tmp/gt_analysis.txt. The analysis is also injected into the instance dict so
   the Jinja2 prompt template can render it inline via {{ instance.gt_analysis }}.
"""
import sys
import os
import re
import textwrap
import subprocess
import json

# ── Config ──────────────────────────────────────────────────────────────
GT_B64_FILE = os.environ["GT_B64_FILE"]
GT_TOOL_PATH = os.environ.get("GT_TOOL_PATH", "")

with open(GT_B64_FILE) as f:
    gt_b64 = f.read().strip()

# Split into 50KB chunks for shell safety
CHUNK_SIZE = 50000
chunks = textwrap.wrap(gt_b64, CHUNK_SIZE)

# Build setup commands: write chunks to temp file, then decode
setup_cmds = ["rm -f /tmp/gt_b64.txt && touch /tmp/gt_b64.txt"]
for chunk in chunks:
    setup_cmds.append(f"echo -n '{chunk}' >> /tmp/gt_b64.txt")
setup_cmds.append(
    "base64 -d /tmp/gt_b64.txt > /tmp/gt_tool.py && chmod +x /tmp/gt_tool.py "
    "&& rm /tmp/gt_b64.txt && echo 'GT tool installed'"
)

# ── Symbol extraction ───────────────────────────────────────────────────

def extract_symbols(problem_statement: str, max_symbols: int = 3) -> list:
    """Extract likely class/symbol names from an issue description."""
    # CamelCase class names
    camel = re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', problem_statement)
    # Dotted references like Model.method → take class part
    dotted = re.findall(r'\b([A-Z][a-zA-Z]*)\.[a-z_]\w*', problem_statement)
    # Backtick-quoted identifiers
    backtick = re.findall(r'`([A-Z][a-zA-Z]+)`', problem_statement)
    # snake_case in backticks
    snake = re.findall(r'`(\w+(?:_\w+)+)`', problem_statement)

    seen = set()
    symbols = []
    for s in camel + dotted + backtick + snake:
        if s not in seen and len(s) > 2 and s not in ('The', 'This', 'That', 'When', 'With'):
            seen.add(s)
            symbols.append(s)
            if len(symbols) >= max_symbols:
                break
    return symbols

# ── Pre-compute GT analysis inside container ────────────────────────────

def run_gt_in_container(container_id: str, command: str, symbol: str = "") -> str:
    """Run gt_tool.py inside a running Docker container."""
    cmd = ["docker", "exec", container_id, "python3", "/tmp/gt_tool.py", command]
    if symbol:
        cmd.append(symbol)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""

def find_container_for_instance(instance_id: str) -> str:
    """Find the Docker container running this SWE-bench instance."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={instance_id}", "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=10,
        )
        cid = result.stdout.strip().split('\n')[0]
        if cid:
            return cid
    except Exception:
        pass

    # Fallback: find latest swebench container
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "ancestor=sweb.eval", "--format", "{{.ID}}", "--latest"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip().split('\n')[0]
    except Exception:
        return ""

def get_gt_analysis(container_id: str, symbols: list) -> str:
    """Run GT analysis in the container for extracted symbols."""
    if not container_id or not symbols:
        return ""

    sections = [f"Symbols detected: {', '.join(symbols)}"]

    # Run groundtruth_impact on top 2 symbols
    for sym in symbols[:2]:
        output = run_gt_in_container(container_id, "groundtruth_impact", sym)
        if output and len(output) > 50:
            sections.append(f"### Impact analysis: {sym}\n{output}")

    # Run groundtruth_references on top symbol
    output = run_gt_in_container(container_id, "groundtruth_references", symbols[0])
    if output and len(output) > 50:
        sections.append(f"### References: {symbols[0]}\n{output}")

    return "\n\n".join(sections) if len(sections) > 1 else ""

# ── Monkey-patch OpenHands ──────────────────────────────────────────────

import benchmarks.swebench.run_infer as run_infer_mod

# Pre-extract symbols for all instances
_symbol_cache = {}

def _cache_symbols(dataset):
    """Pre-extract symbols from all instances in the dataset."""
    for row in dataset:
        iid = row.get("instance_id", "")
        ps = row.get("problem_statement", "")
        if iid and ps:
            _symbol_cache[iid] = extract_symbols(ps)

# Patch 1: Inject gt_tool.py via env_setup_commands
original_init = run_infer_mod.SWEBenchEvaluation.__init__

def patched_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    if self.metadata.env_setup_commands is None:
        self.metadata.env_setup_commands = []
    self.metadata.env_setup_commands.extend(setup_cmds)

    # Also run GT analysis during setup and save to file
    # This runs AFTER gt_tool.py is installed, for the top symbols
    # We add a generic summary command since we don't know the instance yet
    self.metadata.env_setup_commands.append(
        "python3 /tmp/gt_tool.py summary > /tmp/gt_analysis.txt 2>&1 || true"
    )
    print(f"[GT] Injected gt_tool.py via {len(setup_cmds)} setup commands ({len(gt_b64)} bytes)")

    # Cache symbols from dataset if available
    if hasattr(self, 'dataset') and self.dataset is not None:
        _cache_symbols(self.dataset)

run_infer_mod.SWEBenchEvaluation.__init__ = patched_init

# Patch 2: Inject per-instance GT analysis into the instance dict
# This adds gt_analysis field that the Jinja template can render
try:
    # Try to find and patch the method that processes individual instances
    # OpenHands swebench stores instances as dicts — we patch the template rendering
    import jinja2
    _original_render = jinja2.Template.render

    def _patched_render(self, *args, **kwargs):
        """Inject gt_analysis into template context for GT-enabled runs."""
        # Check if this is a swebench prompt render (has 'instance' in context)
        instance = kwargs.get('instance') or (args[0] if args and isinstance(args[0], dict) and 'instance' in args[0] else None)
        if instance is None and args:
            # Sometimes passed as positional dict
            for arg in args:
                if isinstance(arg, dict) and 'problem_statement' in arg:
                    instance = arg
                    break

        if instance and isinstance(instance, dict):
            iid = instance.get('instance_id', '')
            ps = instance.get('problem_statement', '')

            if iid and 'gt_analysis' not in instance:
                # Get cached symbols or extract on the fly
                symbols = _symbol_cache.get(iid) or extract_symbols(ps)
                if symbols:
                    # Build a lightweight analysis string from symbols
                    analysis_parts = [f"Key symbols from the issue: {', '.join(symbols)}"]
                    analysis_parts.append(
                        "Run these commands inside the repo for detailed analysis:"
                    )
                    for sym in symbols[:2]:
                        analysis_parts.append(
                            f"  python3 /tmp/gt_tool.py groundtruth_impact {sym}"
                        )
                    if symbols:
                        analysis_parts.append(
                            f"  python3 /tmp/gt_tool.py groundtruth_references {symbols[0]}"
                        )
                    analysis_parts.append(
                        "\nA codebase summary is available at /tmp/gt_analysis.txt"
                    )
                    instance['gt_analysis'] = '\n'.join(analysis_parts)

        return _original_render(self, *args, **kwargs)

    jinja2.Template.render = _patched_render
    print("[GT] Patched Jinja2 template rendering for per-instance analysis injection")
except Exception as e:
    print(f"[GT] Warning: could not patch Jinja2 rendering: {e}")
    print("[GT] Falling back to env_setup_commands only (analysis in /tmp/gt_analysis.txt)")

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
echo "Mode: Pre-computed analysis injection (no mandatory tool calls)"
echo "Prompt: gt_phase3.j2"
echo "Max iterations: $MAX_ITER"
[ -n "$INSTANCES" ] && echo "Instances: $INSTANCES"
[ -n "$OUTPUT_DIR" ] && echo "Output: $OUTPUT_DIR"
echo ""

# Export paths for the inject script
export GT_B64_FILE
export GT_TOOL_PATH="$GT_TOOL"

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
