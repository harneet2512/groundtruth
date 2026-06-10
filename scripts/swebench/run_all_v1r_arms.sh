#!/usr/bin/env bash
# Run all 4 V1R experiment arms sequentially on the VM.
# Usage: nohup bash /home/Lenovo/v1r_experiment/run_all_v1r_arms.sh > /home/Lenovo/v1r_experiment/run_all.log 2>&1 &
set -euo pipefail
cd /home/Lenovo/oh-benchmarks
source .venv/bin/activate

export OPENAI_API_KEY=$(cat /home/Lenovo/.openrouter_key 2>/dev/null || echo "")
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"

echo "=== V1R Experiment: starting $(date) ==="
echo "OPENAI_BASE_URL=$OPENAI_BASE_URL"
echo ""

# Arm 1: BL (baseline, no GT)
echo "=== ARM: BL ($(date)) ==="
python3 /home/Lenovo/v1r_experiment/run_arm_v1r.py BL 2>&1 | tee /home/Lenovo/v1r_experiment/arm_BL.log
echo "=== BL done ($(date)) ==="
echo ""

# Arm 2: V1 (current production — brief + full hook)
echo "=== ARM: V1 ($(date)) ==="
python3 /home/Lenovo/v1r_experiment/run_arm_v1r.py V1 2>&1 | tee /home/Lenovo/v1r_experiment/arm_V1.log
echo "=== V1 done ($(date)) ==="
echo ""

# Arm 3: V1R-map (refined brief, no hook)
echo "=== ARM: V1R-map ($(date)) ==="
python3 /home/Lenovo/v1r_experiment/run_arm_v1r.py V1R-map 2>&1 | tee /home/Lenovo/v1r_experiment/arm_V1R_map.log
echo "=== V1R-map done ($(date)) ==="
echo ""

# Arm 4: V1R-map+hook (refined brief + lean hook)
echo "=== ARM: V1R-map+hook ($(date)) ==="
python3 /home/Lenovo/v1r_experiment/run_arm_v1r.py "V1R-map+hook" 2>&1 | tee /home/Lenovo/v1r_experiment/arm_V1R_map_hook.log
echo "=== V1R-map+hook done ($(date)) ==="
echo ""

echo "=== V1R Experiment: ALL DONE $(date) ==="
echo "Results in ~/results/v1r_*/"
ls -d ~/results/v1r_* 2>/dev/null
