#!/bin/bash
set -euo pipefail
source ~/sweagent-env/bin/activate
export OPENAI_API_BASE="http://localhost:4000/v1"
export OPENAI_API_KEY="dummy"
export GT_SWEAGENT_DIR="/tmp/SWE-agent"

cd /tmp/Groundtruth_vnext
bash scripts/swebench/preflight_qwen_fc_ablation.sh A
