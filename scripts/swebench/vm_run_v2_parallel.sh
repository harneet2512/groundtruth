#!/bin/bash
set -euo pipefail

# v2 ablation — all 5 arms in parallel, 1 worker each (5 workers on 4 CPU).
# Each arm gets its own isolated output dir. Eval after all complete.

REPO="/tmp/Groundtruth_vnext"
VM_BUNDLE="$REPO/benchmarks/swebench/vm_bundle"
ABLATION_DIR="$REPO/benchmarks/swebench/qwen_fc_ablation"
SWEAGENT="/tmp/SWE-agent"
TIMESTAMP=$(date +%s)
OUTDIR="/tmp/qwen_fc_ablation/v2_parallel_$TIMESTAMP"
TASK_FILE="$REPO/scripts/swebench/frozen_gt_astropy10.txt"

source ~/sweagent-env/bin/activate
export OPENAI_API_BASE="http://localhost:4000/v1"
export OPENAI_API_KEY="dummy"

echo "============================================"
echo "  V2 ABLATION — PARALLEL 5 ARMS"
echo "============================================"
echo "Time:   $(date -u)"
echo "Commit: $(cd $REPO && git rev-parse --short HEAD)"
echo "Output: $OUTDIR"
echo ""

mkdir -p "$OUTDIR"
TASKS=$(paste -sd'|' "$TASK_FILE")

# Copy all configs
for arm in A B C D E; do
    config=$(find "$ABLATION_DIR/configs_v2" -name "${arm}_*.yaml" | head -1)
    [ -n "$config" ] && cp "$config" "$SWEAGENT/config/$(basename $config)"
done

# Set up gt_ablation_v2 bundle with FULL index (default)
bundle="$SWEAGENT/tools/gt_ablation_v2"
rm -rf "$bundle"
mkdir -p "$bundle/bin"
cp "$VM_BUNDLE/install_fc.sh" "$bundle/install.sh"
echo "tools: {}" > "$bundle/config.yaml"
cp "$VM_BUNDLE/swe_agent_state_gt.py" "$bundle/bin/swe_agent_state_gt.py"
cp "$REPO/benchmarks/swebench/gt_intel.py" "$bundle/bin/gt_intel.py"
for f in lsp_promoter.py gt_review_patch.py gt_canary_report.py gt_metrics.py; do
    [ -f "$VM_BUNDLE/$f" ] && cp "$VM_BUNDLE/$f" "$bundle/bin/$f"
done
echo '#!/bin/bash' > "$bundle/bin/_noop"
chmod +x "$bundle/install.sh" "$bundle/bin/"*

cd "$SWEAGENT"

# Launch all 5 arms in parallel — 1 worker each
for arm in A B C D E; do
    config_file=$(find "$SWEAGENT/config" -name "${arm}_*.yaml" | head -1)
    config_basename=$(basename "$config_file")
    arm_dir="$OUTDIR/$arm"
    mkdir -p "$arm_dir"

    # Arm B: swap to noindex install
    if [ "$arm" = "B" ]; then
        cp "$VM_BUNDLE/install_fc_noindex.sh" "$bundle/install.sh"
        chmod +x "$bundle/install.sh"
    elif [ "$arm" != "A" ]; then
        cp "$VM_BUNDLE/install_fc.sh" "$bundle/install.sh"
        chmod +x "$bundle/install.sh"
    fi

    echo "Launching $arm (config=$config_basename)..."
    nohup python3 -m sweagent run-batch \
        --config "config/$config_basename" \
        --instances.subset verified --instances.split test \
        --instances.filter "$TASKS" \
        --output_dir "$arm_dir" --num_workers 1 \
        > "$arm_dir/run.log" 2>&1 &
    echo "  $arm PID=$!"

    # Small delay so B's noindex install doesn't race with C's full install
    sleep 3
done

echo ""
echo "All 5 arms launched. Waiting..."
wait
echo ""
echo "All arms complete. Collecting results..."

# Collect predictions and step counts
for arm in A B C D E; do
    arm_dir="$OUTDIR/$arm"
    echo ""
    echo "=== $arm ==="
    python3 << PYEOF
import json, glob, os
arm_dir = "$arm_dir"
arm = "$arm"
preds = []
for tf in sorted(glob.glob(f"{arm_dir}/astropy*/*.traj")):
    t = json.load(open(tf))
    iid = os.path.basename(os.path.dirname(tf))
    info = t.get("info", {})
    patch = info.get("submission", "") or ""
    steps = len(t.get("trajectory", []))
    preds.append({"instance_id": iid, "model_name_or_path": arm, "model_patch": patch})
    print(f"  {iid}: steps={steps} patch={'YES' if patch.strip() else 'no'}")
with open(f"{arm_dir}/preds.json", "w") as f:
    json.dump(preds, f, indent=2)
print(f"  {arm}: {sum(1 for p in preds if p['model_patch'].strip())}/{len(preds)} patched")
PYEOF
done

# Eval all arms
echo ""
echo "=== EVALUATING ==="
for arm in A B C D E; do
    arm_dir="$OUTDIR/$arm"
    echo "--- $arm ---"
    python3 -m swebench.harness.run_evaluation \
        --predictions_path "$arm_dir/preds.json" \
        --run_id "v2_$arm" --max_workers 2 \
        --dataset princeton-nlp/SWE-bench_Verified 2>&1 | tail -5
    echo ""
done

# Get resolved IDs
echo "=== RESOLVED ==="
cd "$OUTDIR"
python3 << 'PYEOF'
import json, glob, os
for arm in ["A", "B", "C", "D", "E"]:
    for f in glob.glob(f"{arm}/*.v2_{arm}.json") + glob.glob(f"*.v2_{arm}.json"):
        d = json.load(open(f))
        ids = sorted([i.split("-")[-1] for i in d.get("resolved_ids", [])])
        print(f"{arm}: {len(ids)}/10 resolved = {ids}")
        break
    else:
        print(f"{arm}: no eval report")
PYEOF

echo ""
TOTAL_WALL=$(($(date +%s) - TIMESTAMP))
echo "============================================"
echo "  COMPLETE — ${TOTAL_WALL}s wall time"
echo "  Output: $OUTDIR"
echo "============================================"
