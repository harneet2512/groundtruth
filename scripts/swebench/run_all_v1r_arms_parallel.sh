#!/usr/bin/env bash
# Launch all 4 V1R experiment arms IN PARALLEL on a single 16-vCPU VM.
# 4 arms x V1R_WORKERS=2 = 8 concurrent docker containers.
#
# Usage on VM:
#   nohup bash /home/Lenovo/v1r_experiment/run_all_v1r_arms_parallel.sh \
#     > /home/Lenovo/v1r_experiment/run_all_parallel.log 2>&1 < /dev/null &
set -uo pipefail

EXP_DIR=/home/Lenovo/v1r_experiment
mkdir -p "$EXP_DIR"
cd /home/Lenovo/oh-benchmarks
source .venv/bin/activate

export OPENAI_API_KEY=$(cat /home/Lenovo/.openrouter_key 2>/dev/null || echo "")
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"
export V1R_WORKERS=2
export V1R_MAXITER=100

echo "=== V1R Parallel Experiment: starting $(date) ==="
echo "WORKERS=$V1R_WORKERS  ITER=$V1R_MAXITER  ARMS=4 (parallel)"
echo ""

launch_arm () {
  local arm_label="$1"
  local arm_slug
  arm_slug=$(echo "$arm_label" | tr '+ ' '__')
  local logf="$EXP_DIR/arm_${arm_slug}.log"
  echo "[$(date)] LAUNCH arm=$arm_label log=$logf"
  nohup python3 "$EXP_DIR/run_arm_v1r.py" "$arm_label" > "$logf" 2>&1 < /dev/null &
  local pid=$!
  echo "  pid=$pid"
  echo "$pid $arm_label" >> "$EXP_DIR/arm_pids.txt"
}

: > "$EXP_DIR/arm_pids.txt"

launch_arm "BL"
sleep 4
launch_arm "V1"
sleep 4
launch_arm "V1R-map"
sleep 4
launch_arm "V1R-map+hook"

echo ""
echo "=== All 4 arms launched in parallel. PIDs: ==="
cat "$EXP_DIR/arm_pids.txt"
echo ""
echo "=== Waiting for all arms to complete ==="

# Wait for each pid; capture exit code per arm.
while read -r pid arm; do
  if [ -n "$pid" ]; then
    wait "$pid" 2>/dev/null
    rc=$?
    echo "[$(date)] arm=$arm pid=$pid exit=$rc"
  fi
done < "$EXP_DIR/arm_pids.txt"

echo ""
echo "=== V1R Parallel Experiment: ALL DONE $(date) ==="
ls -d /home/Lenovo/results/v1r_* 2>/dev/null
