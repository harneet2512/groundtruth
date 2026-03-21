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

# Create inject script — prompt-only mode (no container modifications)
INJECT_SCRIPT=$(mktemp /tmp/gt_inject_XXXXXX.py)
cat > "$INJECT_SCRIPT" << 'PYEOF'
"""Monkey-patch OpenHands SWE-bench for GT prompt-only injection.

Injects per-instance GT analysis (extracted symbols + commands) into the Jinja2
prompt template via {{ instance.gt_analysis }}. No container modifications —
the env_setup_commands approach caused 30x more failures than baseline due to
134KB base64 echo commands overwhelming container startup.
"""
import sys
import os
import re

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

# Patch 1: Cache symbols from dataset (no env_setup injection)
original_init = run_infer_mod.SWEBenchEvaluation.__init__

def patched_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    # Cache symbols from dataset if available
    if hasattr(self, 'dataset') and self.dataset is not None:
        _cache_symbols(self.dataset)
    print("[GT] Prompt-only mode: analysis injected via Jinja2 template, no container modifications")

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
        # OpenHands calls template.render(context) where context is a dict
        # with 'instance' key containing the SWE-bench instance dict
        instance = None

        # Check kwargs first
        if 'instance' in kwargs and isinstance(kwargs['instance'], dict):
            instance = kwargs['instance']
        # Check positional context dict: template.render({'instance': {...}, ...})
        elif args and isinstance(args[0], dict) and 'instance' in args[0]:
            instance = args[0]['instance']

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
                    instance['gt_analysis'] = '\n'.join(analysis_parts)

        return _original_render(self, *args, **kwargs)

    jinja2.Template.render = _patched_render
    print("[GT] Patched Jinja2 template rendering for per-instance analysis injection")
except Exception as e:
    print(f"[GT] Warning: could not patch Jinja2 rendering: {e}")
    print("[GT] Jinja2 patching failed, GT analysis will not be injected")

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
rm -f "$INJECT_SCRIPT"
[ -n "${SELECT_FILE:-}" ] && rm -f "$SELECT_FILE"
