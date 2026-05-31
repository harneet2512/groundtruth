#!/usr/bin/env bash
###############################################################################
# gitpod_deepswe_run.sh — LIVE, streaming run on the DeepSWE BENCHMARK in Gitpod.
#
# Runs the DeepSWE-native path: Pier + GTMiniSweAgent (GT injected as gt_hook.py)
# on a deepswe-bench/tasks/<task> in Docker, streaming to the terminal.
#
#   NOTE: this is PATH 3 (gt_hook, grep-based) — the DeepSWE benchmark's own
#   integration — NOT the OpenHands "Live Lite" wrapper (Path 2, use gitpod_run.sh
#   for that). Same agent the GHA `deepswe_trial.yml` runs, just streamed live.
#
# Usage (inside a Gitpod terminal):
#   bash railway/gitpod_deepswe_run.sh                       # default go task
#   GT_TASK=<task_id> bash railway/gitpod_deepswe_run.sh      # any deepswe-bench task
#   GT_MODEL=deepseek/deepseek-v4-flash bash railway/gitpod_deepswe_run.sh
#
# Watch it cleanly in a SECOND terminal:
#   bash railway/gitpod_watch.sh /tmp/gt_debug/deepswe_run.log
###############################################################################
set -eo pipefail
export REPO_ROOT="${GITPOD_REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
cd "$REPO_ROOT"

TASK="${GT_TASK:-abs-module-cache-flags}"          # small Go task by default
MODEL="${GT_MODEL:-deepseek/deepseek-v4-flash}"
TASK_DIR="deepswe-bench/tasks/${TASK}"
LOG="/tmp/gt_debug/deepswe_run.log"
mkdir -p /tmp/gt_debug

echo "── preflight (fail fast) ──"
fail=0
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then echo "  ✓ DEEPSEEK_API_KEY set"; else
  echo "  ✗ DEEPSEEK_API_KEY missing  →  gp env DEEPSEEK_API_KEY=sk-xxxx  (then reopen)"; fail=1; fi
if docker info >/dev/null 2>&1; then echo "  ✓ docker daemon up"; else
  echo "  ✗ docker daemon down (Pier needs it for --env docker)"; fail=1; fi
if [ -d "$TASK_DIR" ]; then echo "  ✓ task dir $TASK_DIR"; else
  echo "  ✗ no such task: $TASK_DIR"; echo "    available:"; ls deepswe-bench/tasks/ 2>/dev/null | head -10 | sed 's/^/      /'; fail=1; fi
if command -v python3 >/dev/null 2>&1; then echo "  ✓ python3 ($(python3 -V 2>&1 | awk '{print $2}'))"; else
  echo "  ✗ python3 missing"; fail=1; fi
avail=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)
echo "  • disk free on / : ${avail:-?}G"
[ "${avail:-0}" -lt 20 ] && echo "    ⚠ <20G — the task Docker image may not fit (see GITPOD_LIVE_RUN.md disk note)"
if [ "$fail" = "1" ]; then echo "── PREFLIGHT FAILED — fix the ✗ above. ──"; exit 2; fi
echo "  → preflight OK"

echo "── install pier + groundtruth (first run only) ──"
pip install -q datacurve-pier 2>&1 | tail -1 || echo "  WARN: pier install issue"
pip install -q -e . 2>&1 | tail -1 || true

echo ""
echo "── launching DeepSWE run (Path 3: Pier + gt_hook) ──"
echo "  task = $TASK   model = $MODEL"
echo "  clean live view in a 2nd terminal:  bash railway/gitpod_watch.sh $LOG"
echo ""
pier run \
  -p "$TASK_DIR" \
  --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent \
  --model "$MODEL" \
  --env docker -y \
  --ak config_file=artifact_deepswe/gt_integration/deepswe_gt_pier.yaml \
  2>&1 | tee "$LOG"
