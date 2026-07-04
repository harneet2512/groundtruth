#!/usr/bin/env bash
# Read a job's LIVE log (pushed by live_log_push.sh to gha-live-logs/<task>) via the GitHub API.
# Uses the API not `git fetch` because the local clone can carry corrupt refs (codex turn-diffs)
# that abort fetch with "did not send all necessary objects". The API has no such dependency.
# Usage: scripts/ci/read_live.sh <task> [tail_lines] [repo]
T="${1:?task}"; N="${2:-120}"; REPO="${3:-hbali-stack/groundtruth}"
gh api "repos/${REPO}/contents/live.log?ref=gha-live-logs/${T}" --jq '.content' 2>/dev/null \
  | base64 -d 2>/dev/null | tail -n "$N" \
  || echo "[read_live] no live log yet for $T (job not in trial step, or bridge not pushing)"
