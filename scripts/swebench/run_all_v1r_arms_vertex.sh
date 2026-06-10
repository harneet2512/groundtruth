#!/usr/bin/env bash
# 4-arm V1R smoke on qwen3-coder Vertex MaaS, split across 2 regions
# (us-central1 + us-south1) to dodge per-region quota throttling.
# 4 arms parallel, WORKERS=1 each, 2 concurrent calls per region.
#
# Usage:
#   bash scripts/swebench/run_all_v1r_arms_vertex.sh                # 4-arm comparison
#   SINGLE_ARM=V1R-map bash .../run_all_v1r_arms_vertex.sh          # one arm only
#   V1R_TASK_FILTER=python-babel__babel-1141 SINGLE_ARM=V1R-map bash .../run_all_v1r_arms_vertex.sh  # one task
#
# Env knobs:
#   SINGLE_ARM         — restrict to one arm (BL | V1 | V1R-map | V1R-map+hook)
#   V1R_TASK_FILTER    — comma-separated instance_ids (default: all 15)
#   V1R_WORKERS        — workers per arm (default: 1)
#   V1R_MAXITER        — max agent iterations (default: 100)
#   V1R_LLM_CONFIG     — LLM config block name (default: qwen3_vertex_south)
#   V1R_EXP_DIR        — experiment scratch dir (default: ~/v1r_experiment)
#   V1R_OH_BENCH_DIR   — OH benchmarks dir with .venv (default: ~/oh-benchmarks)
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER="$REPO_DIR/scripts/swebench/run_arm_v1r.py"

EXP_DIR="${V1R_EXP_DIR:-$HOME/v1r_experiment}"
OH_DIR="${V1R_OH_BENCH_DIR:-$HOME/oh-benchmarks}"
mkdir -p "$EXP_DIR"
cd "$OH_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

# Vertex auth: GCE service account picks up automatically.
unset OPENAI_API_KEY OPENAI_BASE_URL 2>/dev/null
export V1R_WORKERS="${V1R_WORKERS:-1}"
export V1R_MAXITER="${V1R_MAXITER:-100}"

# qwen3_vertex_south block has caching_prompt=false, max_output_tokens=8192
# already set. Region is overridden per-arm via VERTEXAI_LOCATION.
export V1R_LLM_CONFIG="${V1R_LLM_CONFIG:-qwen3_vertex_south}"

# Optional task filter passes through to run_arm_v1r.py.
if [ -n "${V1R_TASK_FILTER:-}" ]; then
  export V1R_TASK_FILTER
fi

echo "=== V1R Vertex run: starting $(date) ==="
echo "WORKERS=$V1R_WORKERS  ITER=$V1R_MAXITER  CONFIG=$V1R_LLM_CONFIG"
echo "RUNNER=$RUNNER"
[ -n "${V1R_TASK_FILTER:-}" ] && echo "TASK_FILTER=$V1R_TASK_FILTER"
[ -n "${SINGLE_ARM:-}" ] && echo "SINGLE_ARM=$SINGLE_ARM"
echo ""

launch_arm () {
  local arm_label="$1"
  local region="$2"
  local arm_slug
  arm_slug=$(echo "$arm_label" | tr '+ ' '__')
  local logf="$EXP_DIR/arm_${arm_slug}.log"
  echo "[$(date)] LAUNCH arm=$arm_label region=$region log=$logf"
  (
    export VERTEXAI_LOCATION="$region"
    nohup python3 "$RUNNER" "$arm_label" > "$logf" 2>&1 < /dev/null
  ) &
  local pid=$!
  echo "  pid=$pid"
  echo "$pid $arm_label $region" >> "$EXP_DIR/arm_pids.txt"
}

: > "$EXP_DIR/arm_pids.txt"

if [ -n "${SINGLE_ARM:-}" ]; then
  case "$SINGLE_ARM" in
    BL)            launch_arm "BL"            "us-central1" ;;
    V1)            launch_arm "V1"            "us-central1" ;;
    V1R-map)       launch_arm "V1R-map"       "us-south1"   ;;
    "V1R-map+hook") launch_arm "V1R-map+hook" "us-south1"   ;;
    *) echo "FATAL: unknown SINGLE_ARM=$SINGLE_ARM" >&2; exit 1 ;;
  esac
else
  echo "Regions: BL+V1 -> us-central1 ; V1R-map+V1R-map+hook -> us-south1"
  echo ""
  launch_arm "BL" "us-central1"
  sleep 5
  launch_arm "V1" "us-central1"
  sleep 5
  launch_arm "V1R-map" "us-south1"
  sleep 5
  launch_arm "V1R-map+hook" "us-south1"
fi

echo ""
echo "=== Launched. PIDs / regions: ==="
cat "$EXP_DIR/arm_pids.txt"
echo ""
echo "=== Waiting on each arm ==="

while read -r pid arm region; do
  if [ -n "$pid" ]; then
    wait "$pid" 2>/dev/null
    rc=$?
    echo "[$(date)] arm=$arm region=$region pid=$pid exit=$rc"
  fi
done < "$EXP_DIR/arm_pids.txt"

echo ""
echo "=== V1R Vertex run: ALL DONE $(date) ==="
ls -d "$HOME"/results/v1r_* 2>/dev/null | tail -10
