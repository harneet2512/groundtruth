#!/usr/bin/env bash
###############################################################################
# gitpod_run.sh — launch a LIVE, streaming OH+GT eval run inside Gitpod.
#
# Thin wrapper over railway/codespace_run.sh (the proven Path-2 runner):
#   - PREFLIGHT: fails in ~1s on the things that doom a run (no key / dead docker
#     / no go+gcc / low disk) so you DON'T burn the ~5min bootstrap into a run
#     that was never going to work.
#   - points REPO_ROOT at the Gitpod checkout ($GITPOD_REPO_ROOT)
#   - passes GT_TASK / GT_BASELINE through
#
# Usage (inside a Gitpod terminal):
#   bash railway/gitpod_run.sh                          # default task, GT on
#   GT_TASK=<instance_id> bash railway/gitpod_run.sh     # a specific Live task
#   GT_BASELINE=1 bash railway/gitpod_run.sh             # pure OpenHands (A/B)
#
# Watch it cleanly in a SECOND terminal:  bash railway/gitpod_watch.sh
###############################################################################
set -eo pipefail

export REPO_ROOT="${GITPOD_REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

echo "── preflight (fail fast, before the slow bootstrap) ──"
fail=0
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then echo "  ✓ DEEPSEEK_API_KEY set"; else
  echo "  ✗ DEEPSEEK_API_KEY missing  →  gp env DEEPSEEK_API_KEY=sk-xxxx  (then reopen)  OR  export it"; fail=1; fi
if docker info >/dev/null 2>&1; then echo "  ✓ docker daemon up"; else
  echo "  ✗ docker daemon down  →  wait a few s after the workspace opens, or 'sudo service docker start'"; fail=1; fi
if command -v go  >/dev/null 2>&1; then echo "  ✓ go  ($(go version 2>/dev/null | awk '{print $3}'))"; else
  echo "  ✗ go missing (gt-index won't build)"; fail=1; fi
if command -v gcc >/dev/null 2>&1; then echo "  ✓ gcc (CGO)"; else
  echo "  ✗ gcc missing (CGO build of gt-index fails)"; fail=1; fi
avail=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)
echo "  • disk free on / : ${avail:-?}G"
if [ "${avail:-0}" -lt 20 ]; then
  echo "    ⚠ <20G free — OH runtime (~several GB) + task image may not fit."
  echo "      If a docker pull dies on space: sudo service docker stop;"
  echo "      sudo mv /var/lib/docker /workspace/docker; sudo dockerd --data-root /workspace/docker &"
fi
if [ "$fail" = "1" ]; then
  echo "── PREFLIGHT FAILED — fix the ✗ above. Aborting before the bootstrap. ──"
  exit 2
fi
echo "  → preflight OK"
echo ""
echo "── launching (TIP: open a 2nd terminal and run 'bash railway/gitpod_watch.sh' for a clean live view) ──"
echo "  REPO_ROOT   = $REPO_ROOT"
echo "  GT_TASK     = ${GT_TASK:-beetbox__beets-5495}"
echo "  GT_BASELINE = ${GT_BASELINE:-0}  ($([ "${GT_BASELINE:-0}" = "1" ] && echo 'pure OpenHands, GT OFF' || echo 'GT ON'))"
echo ""

# Stream the full run to this terminal; codespace_run.sh also tee's to
# /tmp/gt_debug/full_run.log (which gitpod_watch.sh tails for the clean view).
exec bash "$REPO_ROOT/railway/codespace_run.sh"
