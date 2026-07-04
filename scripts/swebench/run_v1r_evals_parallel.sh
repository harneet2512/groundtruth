#!/usr/bin/env bash
# Run SWE-bench-Live eval on all 4 V1R arms in parallel.
# Each arm runs eval_infer.py on its own output.jsonl.
# Eval is local docker (no LLM cost), parallelized internally.
set -uo pipefail

EXP_DIR=/home/Lenovo/v1r_experiment
mkdir -p "$EXP_DIR/eval_logs"
cd /home/Lenovo/oh-benchmarks
source .venv/bin/activate

declare -A ARMS=(
  ["BL"]="/home/Lenovo/results/v1r_BL_1777779319/SWE-bench-Live/SWE-bench-Live/CodeActAgent/qwen3-coder-480b-a35b-instruct-maas_maxiter_100_N_v1r_BL_1777779319/output.jsonl"
  ["V1"]="/home/Lenovo/results/v1r_V1_1777779324/SWE-bench-Live/SWE-bench-Live/CodeActAgent/qwen3-coder-480b-a35b-instruct-maas_maxiter_100_N_v1r_V1_1777779324/output.jsonl"
  ["V1R-map"]="/home/Lenovo/results/v1r_V1R-map_1777779329/SWE-bench-Live/SWE-bench-Live/CodeActAgent/qwen3-coder-480b-a35b-instruct-maas_maxiter_100_N_v1r_V1R-map_1777779329/output.jsonl"
  ["V1R-map+hook"]="/home/Lenovo/results/v1r_V1R-map_plus_hook_1777779334/SWE-bench-Live/SWE-bench-Live/CodeActAgent/qwen3-coder-480b-a35b-instruct-maas_maxiter_100_N_v1r_V1R-map_plus_hook_1777779334/output.jsonl"
)

echo "=== V1R Eval (parallel): starting $(date) ==="

: > "$EXP_DIR/eval_pids.txt"

for arm in "${!ARMS[@]}"; do
  jsonl="${ARMS[$arm]}"
  arm_slug=$(echo "$arm" | tr '+ ' '__')
  logf="$EXP_DIR/eval_logs/eval_${arm_slug}.log"
  echo "[$(date)] EVAL arm=$arm jsonl=$jsonl"
  echo "  log=$logf"
  if [ ! -f "$jsonl" ]; then
    echo "  SKIP: file missing"
    continue
  fi
  nohup python3 -m evaluation.benchmarks.swe_bench.eval_infer \
    --input-file "$jsonl" \
    --dataset SWE-bench-Live/SWE-bench-Live \
    --split lite \
    > "$logf" 2>&1 < /dev/null &
  pid=$!
  echo "  pid=$pid"
  echo "$pid $arm" >> "$EXP_DIR/eval_pids.txt"
  sleep 3
done

echo
echo "=== All evals launched ==="
cat "$EXP_DIR/eval_pids.txt"
echo
echo "=== Waiting ==="
while read -r pid arm; do
  if [ -n "$pid" ]; then
    wait "$pid" 2>/dev/null
    rc=$?
    echo "[$(date)] eval arm=$arm pid=$pid exit=$rc"
  fi
done < "$EXP_DIR/eval_pids.txt"

echo
echo "=== V1R Eval: ALL DONE $(date) ==="
ls -la /home/Lenovo/results/v1r_*/SWE-bench-Live/SWE-bench-Live/CodeActAgent/*/output.swebench_eval.jsonl 2>/dev/null
