#!/bin/bash
set -euo pipefail

# Full Qwen FC ablation: run A-E sequentially, eval each, produce comparison.
# Arms sequential (easier attribution), tasks parallel within arm.
# Usage: bash run_qwen_fc_ablation.sh [--arms A,B,C,D,E] [--workers 2]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ABLATION_DIR="$REPO_DIR/benchmarks/swebench/qwen_fc_ablation"
SWEAGENT_DIR="${GT_SWEAGENT_DIR:-/tmp/SWE-agent}"
TIMESTAMP=$(date +%s)
OUTDIR="/tmp/qwen_fc_ablation/run_$TIMESTAMP"
ARMS="${1:-A,B,C,D,E}"
WORKERS="${GT_ABLATION_WORKERS:-2}"
TASK_FILE="$REPO_DIR/scripts/swebench/frozen_gt_astropy10.txt"

source ~/sweagent-env/bin/activate
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://localhost:4000/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-dummy}"

echo "============================================"
echo "  QWEN FC ABLATION — FULL RUN"
echo "============================================"
echo "Time:    $(date -u)"
echo "Commit:  $(cd $REPO_DIR && git rev-parse --short HEAD)"
echo "Arms:    $ARMS"
echo "Workers: $WORKERS"
echo "Tasks:   $(wc -l < $TASK_FILE)"
echo "Output:  $OUTDIR"
echo ""

mkdir -p "$OUTDIR/configs" "$OUTDIR/aggregate"

# Write manifest
cat > "$OUTDIR/manifest.json" << MEOF
{
  "git_commit": "$(cd $REPO_DIR && git rev-parse --short HEAD)",
  "git_dirty": $(cd $REPO_DIR && git diff --quiet 2>/dev/null && echo false || echo true),
  "python_version": "$(python3 --version 2>&1)",
  "sweagent_version": "$(python3 -m sweagent --help 2>&1 | head -1)",
  "hostname": "$(hostname)",
  "cpu_count": $(nproc),
  "ram_mb": $(free -m | awk '/^Mem:/{print $2}'),
  "disk_free_gb": $(df -BG / | tail -1 | awk '{print $4}' | tr -d 'G'),
  "model": "openai/qwen3-coder-480b-a35b-instruct-maas",
  "parser": "function_calling",
  "dataset": "frozen_gt_astropy10",
  "task_count": $(wc -l < $TASK_FILE),
  "workers": $WORKERS,
  "arms": "$(echo $ARMS | tr ',' ' ')",
  "start_timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "timestamp": $TIMESTAMP
}
MEOF

# Copy configs
IFS=',' read -ra ARM_LIST <<< "$ARMS"
for arm in "${ARM_LIST[@]}"; do
    config=$(find "$ABLATION_DIR/configs" -name "${arm}_*.yaml" | head -1)
    if [ -n "$config" ]; then
        cp "$config" "$OUTDIR/configs/"
        cp "$config" "$SWEAGENT_DIR/config/$(basename $config)"
    fi
done

# Set up GT ablation bundle (for B-F arms)
bundle_dir="$SWEAGENT_DIR/tools/gt_ablation"
rm -rf "$bundle_dir"
mkdir -p "$bundle_dir/bin"
cp "$ABLATION_DIR/hooks/install_ablation.sh" "$bundle_dir/install.sh"
cp "$ABLATION_DIR/hooks/ablation_hook.py" "$bundle_dir/ablation_hook.py"
cp "$ABLATION_DIR/hooks/ablation_hook.py" "$bundle_dir/bin/ablation_hook.py"
cp "$ABLATION_DIR/hooks/config.yaml" "$bundle_dir/config.yaml"
if [ -f "$REPO_DIR/benchmarks/swebench/gt_intel.py" ]; then
    cp "$REPO_DIR/benchmarks/swebench/gt_intel.py" "$bundle_dir/gt_intel.py"
    cp "$REPO_DIR/benchmarks/swebench/gt_intel.py" "$bundle_dir/bin/gt_intel.py"
fi
echo '#!/bin/bash' > "$bundle_dir/bin/_noop"
chmod +x "$bundle_dir/install.sh" "$bundle_dir/bin/"*
echo "GT ablation bundle ready"

TASKS=$(paste -sd'|' "$TASK_FILE")
cd "$SWEAGENT_DIR"

# Run each arm sequentially
for arm in "${ARM_LIST[@]}"; do
    config_file=$(find "$OUTDIR/configs" -name "${arm}_*.yaml" | head -1)
    config_basename=$(basename "$config_file")
    arm_dir="$OUTDIR/runs/$arm"
    mkdir -p "$arm_dir/logs"

    echo ""
    echo "============================================"
    echo "  ARM $arm — $(date -u +%H:%M:%S)"
    echo "============================================"

    ARM_START=$(date +%s)

    python3 -m sweagent run-batch \
        --config "config/$config_basename" \
        --instances.subset verified --instances.split test \
        --instances.filter "$TASKS" \
        --output_dir "$arm_dir" --num_workers "$WORKERS" \
        > "$arm_dir/logs/run.log" 2>&1 || true

    ARM_END=$(date +%s)
    ARM_WALL=$((ARM_END - ARM_START))

    # Collect step counts and patches
    echo "  --- Results ---"
    python3 << PYEOF
import json, glob, os

arm_dir = "$arm_dir"
arm = "$arm"
trajs = sorted(glob.glob(f"{arm_dir}/astropy*/*.traj"))
preds = []
summaries = []

for traj_file in trajs:
    with open(traj_file) as f:
        traj = json.load(f)
    iid = os.path.basename(os.path.dirname(traj_file))
    info = traj.get("info", {})
    trajectory = traj.get("trajectory", [])
    patch = info.get("submission", "") or ""
    steps = len(trajectory)
    has_patch = bool(patch.strip())

    preds.append({"instance_id": iid, "model_name_or_path": arm, "model_patch": patch})
    summaries.append({
        "arm": arm, "instance_id": iid, "step_count": steps,
        "patch_created": has_patch, "patch_bytes": len(patch),
        "exit_status": info.get("exit_status", "unknown"),
    })
    status = "YES" if has_patch else "no"
    print(f"  {arm}/{iid}: steps={steps} patch={status}")

# Write predictions
with open(f"{arm_dir}/predictions.jsonl", "w") as f:
    for p in preds:
        f.write(json.dumps(p) + "\n")

# Also write as single JSON array for swebench eval
with open(f"{arm_dir}/preds.json", "w") as f:
    json.dump(preds, f, indent=2)

# Write task summaries
with open(f"{arm_dir}/task_summaries.jsonl", "w") as f:
    for s in summaries:
        f.write(json.dumps(s) + "\n")

patched = sum(1 for s in summaries if s["patch_created"])
total = len(summaries)
print(f"  {arm}: {patched}/{total} patched, wall={$ARM_WALL}s")
PYEOF

    # Run eval
    echo "  --- Evaluating ---"
    python3 -m swebench.harness.run_evaluation \
        --predictions_path "$arm_dir/preds.json" \
        --run_id "$arm" --max_workers "$WORKERS" \
        --dataset princeton-nlp/SWE-bench_Verified \
        > "$arm_dir/logs/eval.log" 2>&1 || true

    # Extract resolved
    REPORT_FILE=$(find "$arm_dir" -name "*.${arm}.json" | head -1)
    if [ -n "$REPORT_FILE" ] && [ -f "$REPORT_FILE" ]; then
        python3 -c "
import json
d = json.load(open('$REPORT_FILE'))
resolved = d.get('resolved_ids', [])
print(f'  $arm: {len(resolved)}/{d.get(\"completed_instances\",\"?\")} resolved')
print(f'  Resolved: {resolved}')
" || echo "  eval parse failed"
    else
        echo "  No eval report found"
    fi

    echo "  ARM $arm complete (${ARM_WALL}s wall)"
done

# Aggregate comparison
echo ""
echo "============================================"
echo "  AGGREGATE COMPARISON"
echo "============================================"

python3 << 'AGGEOF'
import json, glob, os

outdir = os.environ.get("OUTDIR", "")
if not outdir:
    import sys; sys.exit(0)

rows = []
for arm_dir in sorted(glob.glob(f"{outdir}/runs/*")):
    arm = os.path.basename(arm_dir)
    report = None
    for f in glob.glob(f"{arm_dir}/*.{arm}.json"):
        report = json.load(open(f))
        break

    summaries = []
    sf = f"{arm_dir}/task_summaries.jsonl"
    if os.path.exists(sf):
        summaries = [json.loads(l) for l in open(sf) if l.strip()]

    resolved = report.get("resolved_ids", []) if report else []
    completed = report.get("completed_instances", 0) if report else 0
    patched = sum(1 for s in summaries if s.get("patch_created"))
    total = len(summaries)
    avg_steps = sum(s.get("step_count", 0) for s in summaries) / max(total, 1)

    rows.append({
        "arm": arm, "resolved": len(resolved), "completed": completed,
        "patched": patched, "total": total, "avg_steps": round(avg_steps, 1),
        "resolved_ids": resolved,
    })
    print(f"  {arm}: resolved={len(resolved)}/{completed} patched={patched}/{total} avg_steps={avg_steps:.0f}")

# Write comparison CSV
with open(f"{outdir}/aggregate/comparison.csv", "w") as f:
    f.write("arm,resolved,completed,patched,total,avg_steps\n")
    for r in rows:
        f.write(f"{r['arm']},{r['resolved']},{r['completed']},{r['patched']},{r['total']},{r['avg_steps']}\n")

# Write comparison markdown
with open(f"{outdir}/aggregate/comparison.md", "w") as f:
    f.write("# Qwen FC Ablation — Comparison\n\n")
    f.write("| Arm | Resolved | Patched | Avg Steps | Resolved IDs |\n")
    f.write("|---|---|---|---|---|\n")
    for r in rows:
        ids = ", ".join(r["resolved_ids"]) if r["resolved_ids"] else "—"
        f.write(f"| {r['arm']} | {r['resolved']}/{r['completed']} | {r['patched']}/{r['total']} | {r['avg_steps']} | {ids} |\n")

    # Scaffold safety analysis
    f.write("\n## Scaffold Safety\n\n")
    by_arm = {r["arm"]: r for r in rows}
    if "A" in by_arm and "B" in by_arm:
        delta = by_arm["B"]["resolved"] - by_arm["A"]["resolved"]
        verdict = "SAFE" if delta >= 0 else "HARMFUL"
        f.write(f"- A vs B (hook install path): {verdict} (delta={delta:+d})\n")
    if "B" in by_arm and "C" in by_arm:
        delta = by_arm["C"]["resolved"] - by_arm["B"]["resolved"]
        verdict = "SAFE" if delta >= 0 else "HARMFUL"
        f.write(f"- B vs C (evidence computation): {verdict} (delta={delta:+d})\n")
    if "C" in by_arm and "D" in by_arm:
        delta = by_arm["D"]["resolved"] - by_arm["C"]["resolved"]
        signal = "SIGNAL" if delta > 0 else "NO SIGNAL" if delta == 0 else "HARMFUL"
        f.write(f"- C vs D (sibling evidence): {signal} (delta={delta:+d})\n")
    if "C" in by_arm and "E" in by_arm:
        delta = by_arm["E"]["resolved"] - by_arm["C"]["resolved"]
        signal = "SIGNAL" if delta > 0 else "NO SIGNAL" if delta == 0 else "HARMFUL"
        f.write(f"- C vs E (import evidence): {signal} (delta={delta:+d})\n")

    f.write(f"\nNote: n=10, directional signal only.\n")

print()
print(f"Reports written to {outdir}/aggregate/")
AGGEOF

# Final timing
END_TS=$(date +%s)
TOTAL_WALL=$((END_TS - TIMESTAMP))
echo ""
echo "============================================"
echo "  COMPLETE — ${TOTAL_WALL}s total wall time"
echo "  Output: $OUTDIR"
echo "============================================"
