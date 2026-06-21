#!/usr/bin/env bash
# LIVE LOG BRIDGE (file-based, self-contained, no external service).
# Background daemon: every $INTERVAL sec, snapshot the job's full output (step stdout + pier/agent
# output) and FORCE-PUSH it to a throwaway branch  gha-live-logs/<task>  so Claude Code can
# `git fetch` + read it MID-RUN. One branch per matrix task = no parallel-push collisions.
# Force-push keeps the branch at a single ever-replaced commit (bounded churn, code branches clean).
#
# Args: $1=task  $2=step-stdout log (abs)  $3=pier log (abs, trial_output.log)  $4=interval(default 15)
# Best-effort: every failure is swallowed; this NEVER fails the job.
set +e
TASK="${1:?task}"; STEPLOG="${2:-/dev/null}"; PIERLOG="${3:-/dev/null}"; INTERVAL="${4:-15}"
REPO="${GITHUB_REPOSITORY:-}"; TOKEN="${GH_LIVE_TOKEN:-${GITHUB_TOKEN:-}}"   # GHA does NOT auto-export GITHUB_TOKEN; caller passes GH_LIVE_TOKEN
[ -z "$REPO" ] || [ -z "$TOKEN" ] && { echo "[live-log] no repo/token — disabled"; exit 0; }
BRANCH="gha-live-logs/${TASK}"
URL="https://x-access-token:${TOKEN}@github.com/${REPO}.git"
W="$(mktemp -d)"
( cd "$W" && git init -q && git config user.email gha@live && git config user.name gha-live \
    && git checkout -qb live ) 2>/dev/null || exit 0
echo "[live-log] daemon up: branch=$BRANCH every ${INTERVAL}s (read it with: git fetch origin $BRANCH && git show FETCH_HEAD:live.log)"
N=0
while true; do
  N=$((N+1))
  {
    echo "=== LIVE SNAPSHOT #$N | $(date -u +%FT%TZ) | task=$TASK ==="
    echo "--- step stdout (RESMON / [ENV-START] / [gt-shim] / events) [tail 300] ---"
    tail -300 "$STEPLOG" 2>/dev/null
    echo ""
    echo "--- pier / agent output (trial_output.log) [tail 400] ---"
    tail -400 "$PIERLOG" 2>/dev/null
  } > "$W/live.log"
  ( cd "$W" \
      && cp /dev/null .keep 2>/dev/null \
      && git add -A \
      && git commit -qm "live #$N $(date -u +%T)" --allow-empty \
      && git push -qf "$URL" live:"$BRANCH" ) 2>/dev/null || true
  sleep "$INTERVAL"
done
