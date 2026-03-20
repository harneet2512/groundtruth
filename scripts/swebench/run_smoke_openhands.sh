#!/usr/bin/env bash
# Smoke test: 4 tasks × 2 conditions (baseline vs GT) on OpenHands.
#
# Baseline: default.j2 prompt, no gt_tool.py
# GT:       gt_phase3.j2 prompt, gt_tool.py injected via env_setup_commands
#
# Usage:
#   bash scripts/swebench/run_smoke_openhands.sh
#   bash scripts/swebench/run_smoke_openhands.sh --gt-only     # skip baseline
#   bash scripts/swebench/run_smoke_openhands.sh --baseline-only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="${OH_DIR:-$HOME/oh-benchmarks}"

source "$HOME/.local/bin/env" 2>/dev/null || true
source "$HOME/gt-env.sh" 2>/dev/null || true

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="$HOME/oh_smoke_${TIMESTAMP}"
BASELINE_DIR="$OUTPUT_DIR/baseline"
GT_DIR="$OUTPUT_DIR/gt"
mkdir -p "$BASELINE_DIR" "$GT_DIR"

LLM_CONFIG="$OH_DIR/.llm_config/openai_gpt54nano.json"
SMOKE_INSTANCES="$OH_DIR/smoke_instances.txt"

# Ensure smoke instances file exists
if [ ! -f "$SMOKE_INSTANCES" ]; then
    cat > "$SMOKE_INSTANCES" << 'EOF'
django__django-12856
django__django-13158
sympy__sympy-17655
django__django-10914
EOF
fi

echo "=== OpenHands Smoke Test ==="
echo "Output: $OUTPUT_DIR"
echo "LLM: $LLM_CONFIG"
echo "Started: $(date)"

# Check LLM config exists
if [ ! -f "$LLM_CONFIG" ]; then
    echo "ERROR: LLM config not found at $LLM_CONFIG"
    echo "Run openhands_setup_vm.sh first."
    exit 1
fi

# ── Base64-encode gt_tool.py ──────────────────────────────────────────
GT_TOOL="$REPO_DIR/benchmarks/swebench/gt_tool.py"
GT_B64=$(base64 -w0 "$GT_TOOL")
GT_SETUP_CMD="echo '$GT_B64' | base64 -d > /tmp/gt_tool.py && chmod +x /tmp/gt_tool.py"

# Copy GT prompt to OH prompts dir
GT_PROMPT_SRC="$REPO_DIR/benchmarks/swebench/prompts/gt_phase3.j2"
GT_PROMPT_DST="$OH_DIR/benchmarks/swebench/prompts/gt_phase3.j2"
cp "$GT_PROMPT_SRC" "$GT_PROMPT_DST" 2>/dev/null || true

cd "$OH_DIR"

RUN_BASELINE=true
RUN_GT=true
if [ "${1:-}" = "--gt-only" ]; then RUN_BASELINE=false; fi
if [ "${1:-}" = "--baseline-only" ]; then RUN_GT=false; fi

# ── Condition A: Baseline ─────────────────────────────────────────────
if [ "$RUN_BASELINE" = true ]; then
    echo ""
    echo "=== Condition A: Baseline (default prompt, no GT) ==="
    echo "Started: $(date)"

    uv run swebench-infer "$LLM_CONFIG" \
        --dataset princeton-nlp/SWE-bench_Lite --split test \
        --select "$SMOKE_INSTANCES" \
        --workspace docker \
        --max-iterations 300 \
        --num-workers 1 \
        --prompt-path default.j2 \
        --output-dir "$BASELINE_DIR" \
        2>&1 | tee "$BASELINE_DIR/run.log"

    echo "Baseline finished: $(date)"
fi

# ── Condition B: GT Phase 3 ──────────────────────────────────────────
if [ "$RUN_GT" = true ]; then
    echo ""
    echo "=== Condition B: GT Phase 3 (gt_phase3.j2 prompt + gt_tool.py) ==="
    echo "Started: $(date)"

    # Create a wrapper that monkey-patches env_setup_commands
    GT_WRAPPER=$(mktemp /tmp/gt_oh_wrapper_XXXXXX.py)
    cat > "$GT_WRAPPER" << PYEOF
import sys, os
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

# Monkey-patch to inject gt_tool.py
GT_SETUP_CMD = '''$GT_SETUP_CMD'''

import benchmarks.swebench.run_infer as run_infer_mod
_orig_prepare = run_infer_mod.SWEBenchEvaluation.prepare_workspace

def _patched_prepare(self, instance, *args, **kwargs):
    workspace = _orig_prepare(self, instance, *args, **kwargs)
    # Inject gt_tool.py into the workspace
    result = workspace.execute_command(GT_SETUP_CMD)
    if result.exit_code == 0:
        print(f"[GT] gt_tool.py injected into workspace for {instance.id}")
    else:
        print(f"[GT] WARNING: gt_tool.py injection failed for {instance.id}: {result.stderr}")
    return workspace

run_infer_mod.SWEBenchEvaluation.prepare_workspace = _patched_prepare

# Run main
run_infer_mod.main()
PYEOF

    uv run python "$GT_WRAPPER" "$LLM_CONFIG" \
        --dataset princeton-nlp/SWE-bench_Lite --split test \
        --select "$SMOKE_INSTANCES" \
        --workspace docker \
        --max-iterations 300 \
        --num-workers 1 \
        --prompt-path gt_phase3.j2 \
        --output-dir "$GT_DIR" \
        2>&1 | tee "$GT_DIR/run.log"

    rm -f "$GT_WRAPPER"
    echo "GT finished: $(date)"
fi

# ── Smoke Checks ──────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "=== SMOKE TEST CHECKS ==="
echo "============================================"

python3 << 'CHECKEOF'
import os, glob, json, sys

output_dir = os.environ.get("OUTPUT_DIR", sys.argv[1] if len(sys.argv) > 1 else ".")
gt_dir = os.path.join(output_dir, "gt")
baseline_dir = os.path.join(output_dir, "baseline")

passed = 0
failed = 0

def check(name, result):
    global passed, failed
    if result:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name}")
        failed += 1

# Collect all text from GT outputs
gt_text = ""
for pattern in ["**/*.json", "**/*.jsonl", "**/*.log"]:
    for f in glob.glob(os.path.join(gt_dir, pattern), recursive=True):
        try:
            gt_text += open(f).read()
        except Exception:
            pass

bl_text = ""
for pattern in ["**/*.json", "**/*.jsonl", "**/*.log"]:
    for f in glob.glob(os.path.join(baseline_dir, pattern), recursive=True):
        try:
            bl_text += open(f).read()
        except Exception:
            pass

# A: GT tools were used (gt_tool.py commands appear in GT trajectory)
gt_tool_used = any(cmd in gt_text for cmd in [
    "groundtruth_impact", "groundtruth_references", "groundtruth_check"
])
check("A: GT tools used in trajectories", gt_tool_used)

# B: Pattern roles appear in GT output
pattern_roles = ["stores_in_state", "serializes_to_kwargs", "compares_in_eq",
                 "emits_to_output", "passes_to_validator", "reads_in_logic"]
roles_found = sum(1 for r in pattern_roles if r in gt_text)
check(f"B: Pattern roles found ({roles_found}/6)", roles_found > 0)

# C: Completeness check ran
completeness = "COVERED" in gt_text or "MISSED" in gt_text or "groundtruth_check" in gt_text
check("C: Completeness check ran", completeness)

# D: Baseline has NO GT tool calls
bl_has_gt = any(cmd in bl_text for cmd in [
    "groundtruth_impact", "groundtruth_references", "groundtruth_check"
])
check("D: Baseline has no GT calls", not bl_has_gt)

# E: Both conditions produced output
gt_has_output = len(gt_text) > 100
bl_has_output = len(bl_text) > 100
check("E: Both conditions produced output", gt_has_output and bl_has_output)

# F: GT calls bounded (not excessive)
gt_call_count = gt_text.count("gt_tool.py")
check(f"F: GT calls bounded ({gt_call_count} total, expect <20)", gt_call_count < 20)

print(f"\nResult: {passed}/{passed+failed} checks passed")
if failed > 0:
    print("Some checks failed — inspect trajectories before full run.")
CHECKEOF

echo ""
echo "Output: $OUTPUT_DIR"
echo "Finished: $(date)"
