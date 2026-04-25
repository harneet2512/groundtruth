#!/bin/bash
# Launch all 4 arms for GT vNext benchmark comparison
# Contract: benchmarks/swebench/fast_diag/GT_VNEXT_BENCHMARK_CONTRACT.md
#
# Run on the VM AFTER vm_setup_vnext.sh completes.
#
# Usage:
#   bash vm_run_vnext_arms.sh [arm]
#   bash vm_run_vnext_arms.sh B      # run only arm B
#   bash vm_run_vnext_arms.sh all    # run all 4 arms sequentially

set -euo pipefail

REPO="$HOME/Groundtruth"
SUITE="$REPO/scripts/swebench/frozen_gt_astropy10.txt"
OUTBASE="$REPO/benchmarks/swebench/fast_diag/vnext_$(date +%s)"
MODEL="openai/qwen3-coder"
WORKERS=4
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
COMMIT=$(cd "$REPO" && git rev-parse --short HEAD)

cd "$REPO"
source "$REPO/.venv/bin/activate" 2>/dev/null || true

echo "=== GT vNext Benchmark Run ==="
echo "Time:   $TIMESTAMP"
echo "Commit: $COMMIT"
echo "Suite:  frozen_gt_astropy10 (10 tasks)"
echo "Model:  $MODEL"
echo "Output: $OUTBASE"
echo ""

run_arm_b() {
    echo "========================================="
    echo "ARM B: Format-repaired baseline (no GT)"
    echo "========================================="
    local OUTDIR="${OUTBASE}/arm_B_baseline"
    mkdir -p "$OUTDIR"

    # Use baseline config (no GT hook)
    # The key difference: run WITHOUT run_mini_gt_hooked.py — use plain minisweagent
    python -m minisweagent.run.benchmarks.swebench swebench \
        -c "$REPO/benchmarks/swebench/mini_swebench_pro_baseline.yaml" \
        --model "$MODEL" \
        --subset princeton-nlp/SWE-bench_Lite \
        --split test \
        --task-list "$SUITE" \
        -w "$WORKERS" \
        -o "$OUTDIR" \
        2>&1 | tee "$OUTDIR/run.log"

    echo "ARM B complete. Output: $OUTDIR"
    echo ""
}

run_arm_c() {
    echo "========================================="
    echo "ARM C: Shell-only GT (scaffold, no intel)"
    echo "========================================="
    local OUTDIR="${OUTBASE}/arm_C_shell_only"
    mkdir -p "$OUTDIR"

    # Use GT hooked runner but disable evidence
    GT_EVIDENCE_DISABLED=1 python "$REPO/benchmarks/swebench/run_mini_gt_hooked.py" swebench \
        -c "$REPO/benchmarks/swebench/mini_swebench_pro_gt_v10_hooked.yaml" \
        --model "$MODEL" \
        --subset princeton-nlp/SWE-bench_Lite \
        --split test \
        --task-list "$SUITE" \
        -w "$WORKERS" \
        -o "$OUTDIR" \
        2>&1 | tee "$OUTDIR/run.log"

    echo "ARM C complete. Output: $OUTDIR"
    echo ""
}

run_arm_f1() {
    echo "========================================="
    echo "ARM F1: vNext no-LSP (full intelligence)"
    echo "========================================="
    local OUTDIR="${OUTBASE}/arm_F1_vnext_nolsp"
    mkdir -p "$OUTDIR"

    python "$REPO/benchmarks/swebench/run_mini_gt_hooked.py" swebench \
        -c "$REPO/benchmarks/swebench/mini_swebench_pro_gt_v10_hooked.yaml" \
        --model "$MODEL" \
        --subset princeton-nlp/SWE-bench_Lite \
        --split test \
        --task-list "$SUITE" \
        -w "$WORKERS" \
        -o "$OUTDIR" \
        2>&1 | tee "$OUTDIR/run.log"

    echo "ARM F1 complete. Output: $OUTDIR"
    echo ""
}

run_arm_f2() {
    echo "========================================="
    echo "ARM F2: vNext LSP-hybrid"
    echo "========================================="
    local OUTDIR="${OUTBASE}/arm_F2_vnext_lsp"
    mkdir -p "$OUTDIR"

    GT_LSP_ENABLED=1 python "$REPO/benchmarks/swebench/run_mini_gt_hooked.py" swebench \
        -c "$REPO/benchmarks/swebench/mini_swebench_pro_gt_v10_hooked.yaml" \
        --model "$MODEL" \
        --subset princeton-nlp/SWE-bench_Lite \
        --split test \
        --task-list "$SUITE" \
        -w "$WORKERS" \
        -o "$OUTDIR" \
        2>&1 | tee "$OUTDIR/run.log"

    echo "ARM F2 complete. Output: $OUTDIR"
    echo ""
}

run_eval() {
    echo "========================================="
    echo "Running SWE-bench evaluation on all arms"
    echo "========================================="
    for arm_dir in "$OUTBASE"/arm_*; do
        if [ -f "$arm_dir/preds.json" ]; then
            arm_name=$(basename "$arm_dir")
            echo "Evaluating $arm_name..."
            python -m swebench.harness.run_evaluation \
                --predictions_path "$arm_dir/preds.json" \
                --swe_bench_tasks princeton-nlp/SWE-bench_Lite \
                --log_dir "$arm_dir/eval_logs" \
                --testbed /tmp/swebench_eval \
                2>&1 | tee "$arm_dir/eval.log" || true
            echo ""
        fi
    done
}

collect_metrics() {
    echo "========================================="
    echo "Collecting metrics from all arms"
    echo "========================================="

    echo ""
    echo "| Arm | resolved | patched | zero_edit | run_invalid |"
    echo "|---|---|---|---|---|"

    for arm_dir in "$OUTBASE"/arm_*; do
        arm_name=$(basename "$arm_dir" | sed 's/arm_//')
        preds="$arm_dir/preds.json"
        if [ -f "$preds" ]; then
            total=$(python3 -c "import json; d=json.load(open('$preds')); print(len(d))" 2>/dev/null || echo "?")
            patched=$(python3 -c "import json; d=json.load(open('$preds')); print(sum(1 for v in d.values() if v.get('model_patch','').strip()))" 2>/dev/null || echo "?")
            # resolved comes from eval logs
            resolved="(run eval)"
            echo "| $arm_name | $resolved | $patched/$total | ? | ? |"
        fi
    done

    echo ""
    echo "Run 'bash vm_run_vnext_arms.sh eval' after all arms complete."
    echo "Full report: $OUTBASE"
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
        run_eval
        collect_metrics
        ;;
    *)
        echo "Usage: bash vm_run_vnext_arms.sh [B|C|F1|F2|eval|metrics|all]"
        echo ""
        echo "Arms:"
        echo "  B   — Format-repaired baseline (no GT)"
        echo "  C   — Shell-only GT (scaffold, no intelligence)"
        echo "  F1  — vNext no-LSP (full intelligence)"
        echo "  F2  — vNext LSP-hybrid"
        echo "  eval    — Run SWE-bench evaluation on completed arms"
        echo "  metrics — Collect and display metrics"
        echo "  all     — Run all arms sequentially + eval + metrics"
        ;;
esac
