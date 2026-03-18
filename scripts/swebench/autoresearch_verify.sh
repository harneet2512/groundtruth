#!/bin/bash
# Autoresearch verify command: push changes, run 10-task A/B on VM, extract resolve count.
#
# Usage:
#   bash scripts/swebench/autoresearch_verify.sh
#
# Outputs a single integer: GT resolved count out of 10 diagnostic tasks.
# Exit 0 on success, non-zero on infrastructure failure.
#
# Prerequisites:
#   - gcloud CLI authenticated
#   - swebench-ab VM running in us-central1-a
#   - Git remote configured
set -euo pipefail

VM_NAME="swebench-ab"
VM_ZONE="us-central1-a"
REPO_DIR="\$HOME/groundtruth"

echo "--- Autoresearch Verify: Push + Run 10-task A/B ---" >&2

# Step 1: Push current changes
echo "[1/4] Pushing to remote..." >&2
git push origin HEAD 2>&1 >&2 || { echo "ERROR: git push failed" >&2; exit 1; }

# Step 2: Pull on VM and run GT-only diagnostic (faster than full A/B)
echo "[2/4] Running GT diagnostic on VM..." >&2
REMOTE_CMD="cd $REPO_DIR && git pull --ff-only && \
  source \$HOME/gt-venv/bin/activate && \
  eval \"\$(grep '^export.*API_KEY' \$HOME/.bashrc 2>/dev/null)\" && \
  export PYTHONPATH=\"\$HOME/mini-swe-agent/src:\${PYTHONPATH:-}\" && \
  DIAG_DIR=\"benchmarks/swebench/results/autoresearch_\$(date +%Y%m%d_%H%M)\" && \
  mkdir -p \"\$DIAG_DIR\" && \
  TASKS_FILE=\"benchmarks/swebench/diagnostic_tasks.txt\" && \
  FILTER_REGEX=\$(tr -d '\\r' < \"\$TASKS_FILE\" | tr '\\n' '|' | sed 's/|$//') && \
  python3 benchmarks/swebench/run_mini_gt.py \
    -c benchmarks/swebench/mini_swebench_gt_v4.yaml \
    -m openai/gpt-5.4-nano \
    --subset lite --split test \
    --filter \"\$FILTER_REGEX\" \
    -o \"\$DIAG_DIR\" \
    -w 2 2>&1 && \
  echo \"DIAG_DIR=\$DIAG_DIR\" && \
  PREDS=\$(find \"\$DIAG_DIR\" -name 'preds.json' -type f | head -1) && \
  if [ -n \"\$PREDS\" ]; then \
    python3 -m swebench.harness.run_evaluation \
      --predictions_path \"\$PREDS\" \
      --swe_bench_tasks princeton-nlp/SWE-bench_Lite \
      --log_dir \"\$DIAG_DIR/eval_logs\" \
      --testbed /tmp/swebench_testbed \
      --skip_existing \
      --timeout 300 2>&1; \
  fi && \
  echo '---METRIC_START---' && \
  python3 -c \"
import json, sys
from pathlib import Path
diag = Path('\$DIAG_DIR')
eval_dir = diag / 'eval_logs'
resolved = 0
total = 0
if eval_dir.exists():
    for f in eval_dir.rglob('*.json'):
        try:
            d = json.loads(f.read_text())
            total += 1
            if d.get('resolved', False):
                resolved += 1
        except: pass
# Also count from preds for tool usage
preds_files = list(diag.rglob('preds.json'))
tool_calls = 0
adoption = 0
if preds_files:
    pass
print(f'RESOLVED={resolved}')
print(f'TOTAL={total}')
\" && \
  echo '---METRIC_END---'"

# Run on VM with extended timeout (10-task eval takes ~20-40 min)
RESULT=$(gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" --command="$REMOTE_CMD" 2>&1) || {
    echo "ERROR: VM execution failed" >&2
    echo "$RESULT" >&2
    exit 1
}

# Step 3: Extract metric
echo "[3/4] Extracting metric..." >&2
METRIC_BLOCK=$(echo "$RESULT" | sed -n '/---METRIC_START---/,/---METRIC_END---/p')
RESOLVED=$(echo "$METRIC_BLOCK" | grep "^RESOLVED=" | cut -d= -f2)

if [ -z "$RESOLVED" ]; then
    echo "ERROR: Could not extract resolve count from VM output" >&2
    echo "$RESULT" | tail -50 >&2
    exit 1
fi

# Step 4: Output the metric (single number)
echo "[4/4] Result: $RESOLVED resolved out of 10" >&2
echo "$RESOLVED"
