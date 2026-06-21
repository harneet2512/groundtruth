#!/usr/bin/env bash
# Read a job's LIVE log (pushed by live_log_push.sh) without checking out the branch.
# Usage: scripts/ci/read_live.sh <task> [tail_lines]
T="${1:?task}"; N="${2:-80}"
git fetch -q origin "gha-live-logs/${T}" 2>/dev/null \
  && git show FETCH_HEAD:live.log 2>/dev/null | tail -n "$N" \
  || echo "[read_live] no live branch yet for $T (job not started / not instrumented)"
