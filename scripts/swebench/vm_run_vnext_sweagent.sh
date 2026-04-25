#!/usr/bin/env bash
# GT vNext 4-arm benchmark — SWE-agent v1.1.0 + LiteLLM proxy
# Contract: benchmarks/swebench/fast_diag/GT_VNEXT_BENCHMARK_CONTRACT.md
#
# Prerequisites:
#   - LiteLLM proxy running at 172.17.0.1:4000
#   - SWE-agent at /tmp/SWE-agent
#   - gt-index-static built at /tmp/Groundtruth_vnext/gt-index/gt-index-static
#   - sweagent-env at /home/Lenovo/sweagent-env
#
# Usage:
#   bash vm_run_vnext_sweagent.sh B       # arm B only
#   bash vm_run_vnext_sweagent.sh all     # all 4 arms sequentially

set -euo pipefail

export PATH="/home/Lenovo/sweagent-env/bin:/usr/local/go/bin:$PATH"
export OPENAI_API_KEY="sk-gt-local"
export OPENAI_API_BASE="http://172.17.0.1:4000/v1"

SWE_AGENT="/tmp/SWE-agent"
GT_REPO="/tmp/Groundtruth_vnext"
SUITE="$GT_REPO/scripts/swebench/frozen_gt_astropy10.txt"
TIMESTAMP=$(date +%s)
OUTBASE="$GT_REPO/benchmarks/swebench/fast_diag/vnext_${TIMESTAMP}"
COMMIT=$(cd "$GT_REPO" && git rev-parse --short HEAD)

# Read task IDs from frozen suite
TASK_IDS=$(paste -sd',' "$SUITE")

echo "=== GT vNext Benchmark Run ==="
echo "Time:    $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Commit:  $COMMIT"
echo "Suite:   frozen_gt_astropy10 (10 tasks)"
echo "Model:   openai/qwen3-coder-480b-a35b-instruct-maas"
echo "Runner:  SWE-agent v1.1.0"
echo "Proxy:   $OPENAI_API_BASE"
echo "Output:  $OUTBASE"
echo ""

# Preflight
echo "--- Preflight ---"
curl -sf -H "Authorization: Bearer $OPENAI_API_KEY" "$OPENAI_API_BASE/models" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Models: {len(d[\"data\"])} available')" || { echo "ERROR: LiteLLM proxy unreachable"; exit 1; }
test -f "$GT_REPO/gt-index/gt-index-static" || { echo "ERROR: gt-index-static not found"; exit 1; }
echo "OK"
echo ""

run_arm() {
    local ARM_NAME="$1"
    local CONFIG="$2"
    local OUTDIR="${OUTBASE}/arm_${ARM_NAME}"
    local EXTRA_ENV="${3:-}"

    echo "========================================="
    echo "ARM $ARM_NAME"
    echo "Config: $CONFIG"
    echo "Output: $OUTDIR"
    echo "Started: $(date -u)"
    echo "========================================="

    mkdir -p "$OUTDIR"

    # Set arm-specific env vars
    if [ -n "$EXTRA_ENV" ]; then
        eval "export $EXTRA_ENV"
    fi

    cd "$SWE_AGENT"
    sweagent run-batch \
        --config "$CONFIG" \
        --instances.type swe_bench \
        --instances.subset lite \
        --instances.split test \
        --instances.instance_ids ${TASK_IDS//,/ } \
        --output_dir "$OUTDIR" \
        --num_workers 4 \
        2>&1 | tee "$OUTDIR/run.log"

    echo "ARM $ARM_NAME complete: $(date -u)"
    echo ""
}

run_arm_b() {
    run_arm "B_baseline" "$SWE_AGENT/config/canary_nogt_qwen_B.yaml"
}

run_arm_c() {
    run_arm "C_shell_only" "$SWE_AGENT/config/canary_nogt_qwen_C.yaml"
}

run_arm_f1() {
    run_arm "F1_vnext_nolsp" "$SWE_AGENT/config/canary_gt_ds_qwen.yaml"
}

run_arm_f2() {
    run_arm "F2_vnext_lsp" "$SWE_AGENT/config/canary_gt_ds_lsp_qwen.yaml"
}

run_eval() {
    echo "========================================="
    echo "Running SWE-bench evaluation on all arms"
    echo "========================================="

    # Build Docker images first
    echo "Building Docker test images..."
    python3 -m swebench.harness.prepare_images \
        --dataset_name princeton-nlp/SWE-bench_Lite \
        --split test \
        --instance_ids ${TASK_IDS//,/ } \
        --max_workers 4 \
        2>&1 | tee "$OUTBASE/docker_build.log"

    for arm_dir in "$OUTBASE"/arm_*; do
        arm_name=$(basename "$arm_dir")
        preds="$arm_dir/preds.json"
        if [ ! -f "$preds" ]; then
            # Try to build preds from trajectories
            echo "Building preds.json for $arm_name..."
            python3 -c "
import json, glob, os
preds = {}
for traj in glob.glob(os.path.join('$arm_dir', '**', '*.traj'), recursive=True):
    with open(traj) as f:
        data = json.load(f)
    iid = data.get('instance_id', '')
    patch = data.get('info', {}).get('submission', '')
    if iid:
        preds[iid] = {'model_patch': patch, 'instance_id': iid, 'model_name_or_path': 'qwen3-coder'}
if preds:
    with open('$preds', 'w') as f:
        json.dump(preds, f, indent=2)
    print(f'{arm_name}: {len(preds)} predictions')
else:
    print(f'{arm_name}: no trajectories found')
" 2>/dev/null || true
        fi

        if [ -f "$preds" ]; then
            echo "Evaluating $arm_name..."
            python3 -m swebench.harness.run_evaluation \
                --predictions_path "$preds" \
                --swe_bench_tasks princeton-nlp/SWE-bench_Lite \
                --log_dir "$arm_dir/eval_logs" \
                --testbed /tmp/swebench_eval \
                2>&1 | tee "$arm_dir/eval.log" || true
        fi
    done
}

collect_metrics() {
    echo ""
    echo "========================================="
    echo "Metrics Summary"
    echo "========================================="

    python3 - "$OUTBASE" <<'METRICS_PY'
import json, glob, os, sys

base = sys.argv[1]
arms = sorted(glob.glob(os.path.join(base, "arm_*")))

print(f"\n| Arm | resolved | patched | zero_edit | run_invalid |")
print(f"|---|---|---|---|---|")

for arm_dir in arms:
    name = os.path.basename(arm_dir).replace("arm_", "")

    # Count from trajectories
    trajs = glob.glob(os.path.join(arm_dir, "**", "*.traj"), recursive=True)
    patched = 0
    zero_edit = 0
    total = len(trajs)
    for t in trajs:
        try:
            with open(t) as f:
                data = json.load(f)
            patch = data.get("info", {}).get("submission", "")
            if patch and patch.strip():
                patched += 1
            else:
                zero_edit += 1
        except Exception:
            pass

    # Check eval results
    report = os.path.join(arm_dir, "eval_logs", "report.json")
    resolved = "?"
    if os.path.exists(report):
        try:
            with open(report) as f:
                r = json.load(f)
            resolved = sum(1 for v in r.values() if v.get("resolved", False))
        except Exception:
            pass

    print(f"| {name} | {resolved} | {patched}/{total} | {zero_edit}/{total} | ? |")

print()
METRICS_PY
}

# ── Main dispatch ──
ARM="${1:-help}"

case "$ARM" in
    B|b)       run_arm_b ;;
    C|c)       run_arm_c ;;
    F1|f1)     run_arm_f1 ;;
    F2|f2)     run_arm_f2 ;;
    eval)      run_eval ;;
    metrics)   collect_metrics ;;
    all)
        run_arm_b
        run_arm_c
        run_arm_f1
        run_arm_f2
        echo ""
        echo "All arms complete. Running eval..."
        run_eval
        collect_metrics
        ;;
    *)
        echo "Usage: bash vm_run_vnext_sweagent.sh [B|C|F1|F2|eval|metrics|all]"
        echo ""
        echo "Arms:"
        echo "  B   — Format-repaired baseline (no GT)"
        echo "  C   — Shell-only GT (scaffold, no intelligence)"
        echo "  F1  — vNext no-LSP (full intelligence)"
        echo "  F2  — vNext LSP-hybrid"
        echo "  eval    — Run SWE-bench evaluation on completed arms"
        echo "  metrics — Collect and display metrics"
        echo "  all     — Run all 4 arms + eval + metrics"
        ;;
esac
