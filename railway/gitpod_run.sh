#!/usr/bin/env bash
###############################################################################
# gitpod_run.sh — launch a LIVE, streaming OH+GT eval run inside Gitpod.
#
# Thin wrapper over railway/codespace_run.sh (the proven Path-2 runner):
#   - points REPO_ROOT at the Gitpod checkout ($GITPOD_REPO_ROOT)
#   - passes GT_TASK / GT_BASELINE through
#   - guards/announces disk (OH runtime + task images are multi-GB)
#
# Usage (inside a Gitpod terminal):
#   bash railway/gitpod_run.sh                          # default task, GT on
#   GT_TASK=<instance_id> bash railway/gitpod_run.sh     # a specific Live task
#   GT_BASELINE=1 bash railway/gitpod_run.sh             # pure OpenHands (A/B)
#
# Requires: DEEPSEEK_API_KEY in the environment (set via `gp env` or `export`).
###############################################################################
set -eo pipefail

export REPO_ROOT="${GITPOD_REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "FATAL: DEEPSEEK_API_KEY is not set."
  echo "  Set it once (persists):  gp env DEEPSEEK_API_KEY=sk-xxxx   (then reopen)"
  echo "  Or for this shell:       export DEEPSEEK_API_KEY=sk-xxxx"
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "FATAL: docker daemon not reachable. In gitpod/workspace-full it should be"
  echo "       auto-started; wait a few seconds after the workspace opens, or run"
  echo "       'sudo service docker start' / re-open the workspace, then retry."
  exit 1
fi

# Disk note: the OH runtime image (~several GB) + the task image are large.
# If a 'docker pull' fails on space, move Docker's data-root to the roomier
# mount (mirrors the Codespaces fix), e.g.:
#   sudo systemctl stop docker; sudo mv /var/lib/docker /workspace/docker
#   sudo dockerd --data-root /workspace/docker &   # (or edit /etc/docker/daemon.json)
echo "── disk before run ──"; df -h / /workspace 2>/dev/null | sed 's/^/  /'

echo "── launching ──"
echo "  REPO_ROOT   = $REPO_ROOT"
echo "  GT_TASK     = ${GT_TASK:-beetbox__beets-5495}"
echo "  GT_BASELINE = ${GT_BASELINE:-0}  ($([ "${GT_BASELINE:-0}" = "1" ] && echo 'pure OpenHands, GT OFF' || echo 'GT ON'))"
echo ""

# Stream the full run to the terminal. codespace_run.sh also tee's to its own log.
exec bash "$REPO_ROOT/railway/codespace_run.sh"
