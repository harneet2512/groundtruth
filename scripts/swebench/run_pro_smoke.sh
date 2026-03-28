#!/bin/bash
set -euo pipefail

# =============================================================================
# GT v10 SWE-bench Pro Smoke Test
#
# Runs 5 Python tasks from SWE-bench Pro (ansible repo) through:
#   1. Baseline (no GT) — clean control
#   2. GT v10 ego-graph precompute — cross-file intelligence
#
# Key differences from SWE-bench Lite:
#   - Dataset: ScaleAI/SWE-bench_Pro (not princeton-nlp/SWE-bench_Lite)
#   - Docker images: jefzda/sweap-images:{dockerhub_tag} (not swebench/sweb.eval...)
#   - Repo path in container: /app (not /testbed)
#   - Instance IDs are longer (e.g. ansible__ansible-12345)
#
# Prerequisites:
#   - gt-venv with mini-swe-agent installed
#   - litellm proxy running on port 4000
#   - Docker access for pulling sweap images
#
# Usage:
#   bash run_pro_smoke.sh [--tasks 5] [--workers 2] [--gt-only] [--baseline-only]
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

NUM_TASKS=5
NUM_WORKERS=2
MODEL="openai/qwen3-coder"
RUN_BASELINE=true
RUN_GT=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tasks)          NUM_TASKS="$2";     shift 2 ;;
        --workers)        NUM_WORKERS="$2";   shift 2 ;;
        --model)          MODEL="$2";         shift 2 ;;
        --gt-only)        RUN_BASELINE=false;  shift ;;
        --baseline-only)  RUN_GT=false;        shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

OUTPUT_ROOT="$HOME/results/v10_pro_smoke_${TIMESTAMP}"
BASELINE_OUT="$OUTPUT_ROOT/baseline"
GT_OUT="$OUTPUT_ROOT/gt_v10"

mkdir -p "$OUTPUT_ROOT" "$BASELINE_OUT" "$GT_OUT"

# ── Config paths ─────────────────────────────────────────────────────
BASELINE_CONFIG="$REPO_DIR/benchmarks/swebench/mini_swebench_pro_baseline.yaml"
GT_CONFIG="$REPO_DIR/benchmarks/swebench/mini_swebench_pro_gt_v10.yaml"
GT_RUNNER="$REPO_DIR/benchmarks/swebench/run_mini_gt_pro_v10.py"
BASELINE_RUNNER="$REPO_DIR/benchmarks/swebench/run_v7_baseline.py"
GT_HOOK="$REPO_DIR/benchmarks/swebench/gt_hook.py"

# ── Validate files exist ─────────────────────────────────────────────
for f in "$BASELINE_CONFIG" "$GT_CONFIG" "$GT_RUNNER" "$BASELINE_RUNNER"; do
    if [ ! -f "$f" ]; then
        echo "ERROR: Required file not found: $f"
        exit 1
    fi
done
if $RUN_GT && [ ! -f "$GT_HOOK" ]; then
    echo "ERROR: gt_hook.py not found at $GT_HOOK"
    exit 1
fi

# ── Activate environment ─────────────────────────────────────────────
if [ -f "$HOME/gt-venv/bin/activate" ]; then
    source "$HOME/gt-venv/bin/activate"
fi
if [ -f "$HOME/gt-env.sh" ]; then
    source "$HOME/gt-env.sh"
fi

export OPENAI_API_KEY="${OPENAI_API_KEY:-dummy}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://localhost:4000/v1}"
export MSWEA_COST_TRACKING=ignore_errors
export PATH="$HOME/.local/bin:$PATH"

# ── Preflight checks ────────────────────────────────────────────────
echo "=== Preflight checks ==="

# Check litellm proxy
if ! curl -s --max-time 5 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running on port 4000."
    echo "Start it first."
    exit 1
fi
echo "  Proxy: OK"

# Check Docker
if ! docker info > /dev/null 2>&1; then
    echo "ERROR: Docker not accessible."
    exit 1
fi
echo "  Docker: OK"

if $RUN_GT; then
    echo "  gt_hook.py: $(wc -c < "$GT_HOOK") bytes"
fi

# ── Pull Docker images for the 5 tasks ──────────────────────────────
echo ""
echo "=== Pulling SWE-bench Pro Docker images ==="

# Use Python to load the dataset and get dockerhub tags for the first N tasks
# Filter for ansible repo (Python, GT-compatible)
PULL_SCRIPT=$(cat << 'PYEOF'
import sys, json

num_tasks = int(sys.argv[1])

try:
    from datasets import load_dataset
    ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
except Exception as e:
    print(f"ERROR: Cannot load SWE-bench Pro dataset: {e}", file=sys.stderr)
    sys.exit(1)

# Filter for Python repos (ansible, qutebrowser are Python)
python_repos = {"ansible/ansible", "qutebrowser/qutebrowser"}
tasks = []
for row in ds:
    if row["repo"] in python_repos:
        tasks.append({
            "instance_id": row["instance_id"],
            "repo": row["repo"],
            "dockerhub_tag": row.get("dockerhub_tag", ""),
        })
    if len(tasks) >= num_tasks:
        break

if not tasks:
    # Fallback: take first N tasks regardless of repo
    for i in range(min(num_tasks, len(ds))):
        row = ds[i]
        tasks.append({
            "instance_id": row["instance_id"],
            "repo": row["repo"],
            "dockerhub_tag": row.get("dockerhub_tag", ""),
        })

json.dump(tasks, sys.stdout)
PYEOF
)

TASKS_JSON=$(python3 -c "$PULL_SCRIPT" "$NUM_TASKS")
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to load SWE-bench Pro tasks."
    exit 1
fi

echo "$TASKS_JSON" | python3 -c "
import sys, json
tasks = json.load(sys.stdin)
print(f'Selected {len(tasks)} tasks:')
for t in tasks:
    print(f\"  {t['instance_id']} (repo={t['repo']}, tag={t['dockerhub_tag']})\")
"

# Save task list for reference
echo "$TASKS_JSON" > "$OUTPUT_ROOT/tasks.json"

# Pull images
echo ""
echo "Pulling Docker images..."
echo "$TASKS_JSON" | python3 -c "
import sys, json, subprocess
tasks = json.load(sys.stdin)
for t in tasks:
    tag = t['dockerhub_tag']
    if not tag:
        print(f\"  SKIP {t['instance_id']}: no dockerhub_tag\")
        continue
    image = f'jefzda/sweap-images:{tag}'
    print(f'  Pulling {image}...')
    r = subprocess.run(['docker', 'pull', image], capture_output=True, text=True)
    if r.returncode == 0:
        print(f'    OK')
    else:
        print(f'    WARN: pull failed: {r.stderr.strip()[:100]}')
"

# Extract instance IDs for the slice
INSTANCE_IDS=$(echo "$TASKS_JSON" | python3 -c "
import sys, json
tasks = json.load(sys.stdin)
for t in tasks:
    print(t['instance_id'])
")

# Write instance file for mini-swe-agent
INSTANCE_FILE="$OUTPUT_ROOT/instances.txt"
echo "$INSTANCE_IDS" > "$INSTANCE_FILE"
TASK_COUNT=$(wc -l < "$INSTANCE_FILE")

echo ""
echo "============================================================="
echo "  GT v10 SWE-bench Pro Smoke Test"
echo "  Started:    $(date -u) UTC"
echo "  Output:     $OUTPUT_ROOT"
echo "  Workers:    $NUM_WORKERS"
echo "  Tasks:      $TASK_COUNT"
echo "  Model:      $MODEL"
echo "  Baseline:   $RUN_BASELINE"
echo "  GT v10:     $RUN_GT"
echo "============================================================="
echo ""

# ── Condition 1: Baseline (no GT) ───────────────────────────────────
if $RUN_BASELINE; then
    echo "============================================================="
    echo "  CONDITION 1: Baseline (no GroundTruth)"
    echo "  Started: $(date -u) UTC"
    echo "============================================================="
    echo ""

    cd "$REPO_DIR"
    python3 "$BASELINE_RUNNER" \
        -c "$BASELINE_CONFIG" \
        --model "$MODEL" \
        --subset ScaleAI/SWE-bench_Pro \
        --split test \
        --slice "0:$NUM_TASKS" \
        -w "$NUM_WORKERS" \
        -o "$BASELINE_OUT" \
        2>&1 | tee "$BASELINE_OUT/run.log" || true

    echo ""
    echo "  Baseline complete: $(date -u) UTC"
    echo "  Output: $BASELINE_OUT"
    echo ""
fi

# ── Condition 2: GT v10 ego-graph precompute ─────────────────────────
if $RUN_GT; then
    echo "============================================================="
    echo "  CONDITION 2: GT v10 Ego-Graph Precompute"
    echo "  Started: $(date -u) UTC"
    echo "============================================================="
    echo ""

    cd "$REPO_DIR"
    python3 "$GT_RUNNER" \
        -c "$GT_CONFIG" \
        --model "$MODEL" \
        --subset ScaleAI/SWE-bench_Pro \
        --split test \
        --slice "0:$NUM_TASKS" \
        -w "$NUM_WORKERS" \
        -o "$GT_OUT" \
        2>&1 | tee "$GT_OUT/run.log" || true

    echo ""
    echo "  GT v10 complete: $(date -u) UTC"
    echo "  Output: $GT_OUT"
    echo ""

    # Extract hook logs
    echo "=== Extracting GT hook logs ==="
    GT_LOG_DIR="$GT_OUT/gt_logs"
    mkdir -p "$GT_LOG_DIR"

    for container_id in $(docker ps -a --filter "label=mini-swe-agent" -q 2>/dev/null); do
        instance_name=$(docker inspect --format '{{.Name}}' "$container_id" | tr -d '/')
        docker cp "$container_id:/tmp/gt_hook_log.jsonl" "$GT_LOG_DIR/${instance_name}.jsonl" 2>/dev/null && \
            echo "  Extracted: ${instance_name}" || true
    done

    if [ -d "$GT_LOG_DIR" ] && [ "$(ls -A "$GT_LOG_DIR" 2>/dev/null)" ]; then
        echo ""
        echo "=== GT v10 Hook Analysis ==="
        python3 "$SCRIPT_DIR/analyze_hook_logs.py" "$GT_LOG_DIR" --detail 2>/dev/null || true
    fi
fi

# ── Comparison ───────────────────────────────────────────────────────
echo ""
echo "============================================================="
echo "  RESULTS COMPARISON"
echo "  Finished: $(date -u) UTC"
echo "============================================================="
echo ""
echo "Output root: $OUTPUT_ROOT"
echo ""

# Show predictions side by side
python3 << 'COMPARE_EOF'
import json, sys, os

output_root = os.environ.get("OUTPUT_ROOT", "")
if not output_root:
    sys.exit(0)

baseline_preds = os.path.join(output_root, "baseline", "preds.json")
gt_preds = os.path.join(output_root, "gt_v10", "preds.json")

def load_preds(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        # preds.json is {instance_id: {model_name_or_patch: ...}}
        return data
    except Exception:
        return {}

baseline = load_preds(baseline_preds)
gt = load_preds(gt_preds)

all_ids = sorted(set(list(baseline.keys()) + list(gt.keys())))
if not all_ids:
    print("No predictions found yet. Check run logs.")
    sys.exit(0)

print(f"{'Instance ID':<60} {'Baseline':>10} {'GT v10':>10}")
print("-" * 82)

baseline_patches = 0
gt_patches = 0

for iid in all_ids:
    b_status = "patch" if baseline.get(iid) else "---"
    g_status = "patch" if gt.get(iid) else "---"
    if b_status == "patch":
        baseline_patches += 1
    if g_status == "patch":
        gt_patches += 1
    print(f"{iid:<60} {b_status:>10} {g_status:>10}")

print("-" * 82)
print(f"{'Patches produced':<60} {baseline_patches:>10} {gt_patches:>10}")
print()
print("NOTE: Patches produced != resolved. Run eval to check correctness.")
print(f"  Baseline dir: {os.path.join(output_root, 'baseline')}")
print(f"  GT v10 dir:   {os.path.join(output_root, 'gt_v10')}")
COMPARE_EOF

echo ""
echo "============================================================="
echo "  Pro smoke test complete: $(date -u) UTC"
echo "============================================================="
echo ""
echo "Next steps:"
echo "  1. Review logs:  less $OUTPUT_ROOT/baseline/run.log"
echo "  2. Review logs:  less $OUTPUT_ROOT/gt_v10/run.log"
echo "  3. Run eval (requires SWE-bench Pro eval harness)"
echo ""
