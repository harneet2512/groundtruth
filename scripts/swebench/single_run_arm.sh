#!/usr/bin/env bash
# single_run_arm.sh — minimal 10-task single-run driver for ONE arm.
#
# Rationale: finalize_gt_preflight.sh's run_task() hardcodes GT_ARM=gt-nolsp
# (line 128). To run a same-bundle 10-task single run for the lsp-hybrid arm
# without modifying the canary-frozen launcher, this script replicates the
# same run_task semantics verbatim with GT_ARM parameterized.
#
# Invocation (on VM):
#   bash single_run_arm.sh <arm> <config_path> <out_root>
#   e.g. bash single_run_arm.sh gt-lsp-hybrid \
#          /tmp/SWE-agent/config/canary_gt_ds_lsp.yaml \
#          /tmp/gt_single_lsp
#
# Assumes parity already verified on VM (top-level + bin/).
set -euo pipefail

GT_ARM_ARG="${1:?arm name required: gt-nolsp or gt-lsp-hybrid}"
CONFIG="${2:?config path required}"
OUT_ROOT="${3:?out_root required}"
SUITE_FILE="${SUITE_FILE:-/tmp/gt_reset_ladder/frozen_gt_astropy10.txt}"
HOST_GT_REPO_SRC="${HOST_GT_REPO_SRC:-/home/Lenovo/SWE-agent/tools/groundtruth}"
VM_SWEAGENT_DIR="${VM_SWEAGENT_DIR:-/tmp/SWE-agent}"
ACTIVATE="${ACTIVATE:-/home/Lenovo/sweagent-env/bin/activate}"
ENV_FILE="${ENV_FILE:-/tmp/bedrock.env}"
MODEL="${MODEL:-openai/deepseek-v3.2-maas}"

# Source sweagent env
# shellcheck disable=SC1090
source "$ACTIVATE"
# shellcheck disable=SC1090
source "$ENV_FILE" 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"
export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-gt-local}"
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://172.17.0.1:4000}"
# LSP enable flag follows arm (hook itself decides via GT_LSP_ENABLED)
if [[ "$GT_ARM_ARG" == "gt-lsp-hybrid" ]]; then
  export GT_LSP_ENABLED=1
else
  export GT_LSP_ENABLED=0
fi

SUITE_TASKS="$(tr '\n' ' ' < "$SUITE_FILE" | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"

echo "=== single_run_arm: $GT_ARM_ARG ==="
echo "config: $CONFIG"
echo "out: $OUT_ROOT"
echo "tasks: $SUITE_TASKS"

mkdir -p "$OUT_ROOT"

# Launch telemetry scraper sidecar for this arm's OUTDIR
bash "$HOST_GT_REPO_SRC/gt_telemetry_scraper.sh" "$OUT_ROOT" >"$OUT_ROOT/scraper.log" 2>&1 &
SCRAPER_PID=$!
trap 'kill $SCRAPER_PID 2>/dev/null || true' EXIT

# run_task mirrors finalize_gt_preflight.sh's run_task, with GT_ARM parameterized.
run_task() {
  local task="$1"
  local root="$2"
  local cfg="$3"
  mkdir -p "$root/$task"
  export GT_ARM="$GT_ARM_ARG"
  export GT_RUN_ID="single_${task}_$(date +%s)"
  export GT_INSTANCE_ID="$task"
  export GT_TELEMETRY_DIR="$root/$task"
  export GT_ARM_ON_MATERIAL_EDIT=1
  local task_bundle="$root/$task/groundtruth_bundle"
  rm -rf "$task_bundle"
  mkdir -p "$task_bundle"
  cp -a "$HOST_GT_REPO_SRC/." "$task_bundle"/
  mkdir -p "$task_bundle/src"
  cp -a /home/Lenovo/groundtruth_src/groundtruth "$task_bundle/src/" 2>/dev/null || true
  mkdir -p "$task_bundle/bin"
  cat > "$task_bundle/bin/gt_identity.env" <<IDENTITYEOF
GT_ARM=$GT_ARM
GT_RUN_ID=$GT_RUN_ID
GT_INSTANCE_ID=$GT_INSTANCE_ID
GT_TELEMETRY_DIR=$GT_TELEMETRY_DIR
IDENTITYEOF
  cat > "$task_bundle/bin/gt_budget.state.json" <<BUDGETEOF
{"scope":"${GT_RUN_ID}__${GT_INSTANCE_ID}__${GT_ARM}","orient":{"count":0,"limit":1,"exhausted":false},"lookup":{"count":0,"limit":2,"exhausted":false},"impact":{"count":0,"limit":2,"exhausted":false},"check":{"count":0,"limit":3,"exhausted":false},"orient_exhausted":false,"initialized":true,"source":"single_run_arm"}
BUDGETEOF
  cat > "$task_bundle/bin/gt_startup_trace.jsonl" <<TRACEEOF
{"event":"startup_enter","ts":0,"scope":"${GT_RUN_ID}__${GT_INSTANCE_ID}__${GT_ARM}","run_id":"$GT_RUN_ID","arm":"$GT_ARM","instance_id":"$GT_INSTANCE_ID","source":"single_run_arm"}
{"event":"identity_written","ts":0,"scope":"${GT_RUN_ID}__${GT_INSTANCE_ID}__${GT_ARM}","run_id":"$GT_RUN_ID","arm":"$GT_ARM","instance_id":"$GT_INSTANCE_ID","identity_present":true,"source":"single_run_arm"}
{"event":"budget_written","ts":0,"scope":"${GT_RUN_ID}__${GT_INSTANCE_ID}__${GT_ARM}","run_id":"$GT_RUN_ID","arm":"$GT_ARM","instance_id":"$GT_INSTANCE_ID","budget_state_present":true,"source":"single_run_arm"}
{"event":"telemetry_ready","ts":0,"scope":"${GT_RUN_ID}__${GT_INSTANCE_ID}__${GT_ARM}","run_id":"$GT_RUN_ID","arm":"$GT_ARM","instance_id":"$GT_INSTANCE_ID","telemetry_ready":true,"source":"single_run_arm"}
TRACEEOF
  local patched_cfg="$root/$task/cfg.yaml"
  python3 - "$cfg" "$patched_cfg" "$GT_ARM" "$GT_RUN_ID" "$GT_INSTANCE_ID" "$GT_TELEMETRY_DIR" "$task_bundle" <<'PY'
import sys, yaml
src, dst, arm, run_id, iid, tdir, bundle_path = sys.argv[1:8]
with open(src) as f:
    cfg = yaml.safe_load(f)
env = cfg["agent"]["tools"].setdefault("env_variables", {})
env["GT_ARM"] = arm
env["GT_RUN_ID"] = run_id
env["GT_INSTANCE_ID"] = iid
env["GT_TELEMETRY_DIR"] = tdir
env["GT_ARM_ON_MATERIAL_EDIT"] = "1"
for bundle in cfg["agent"]["tools"].get("bundles", []):
    if isinstance(bundle, dict) and bundle.get("path", "").endswith("groundtruth"):
        bundle["path"] = bundle_path
with open(dst, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
PY
  cd "$VM_SWEAGENT_DIR"
  python3 -m sweagent run-batch \
    --config "$patched_cfg" \
    --instances.type swe_bench --instances.subset verified --instances.split test \
    --instances.filter "$task" \
    --output_dir "$root/$task" \
    > "$root/$task/run.log" 2>&1
}

for task in $SUITE_TASKS; do
  echo "[$(date +%H:%M:%S)] --- task: $task ---"
  run_task "$task" "$OUT_ROOT" "$CONFIG" || echo "[$(date +%H:%M:%S)] task $task returned non-zero; continuing"
  # Brief scrape sync after each task to catch any final telemetry before container teardown
  bash "$HOST_GT_REPO_SRC/gt_telemetry_scraper.sh" "$OUT_ROOT" --once >/dev/null 2>&1 || true
done

echo "[$(date +%H:%M:%S)] === $GT_ARM_ARG single run DONE ==="
