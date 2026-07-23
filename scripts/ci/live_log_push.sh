#!/usr/bin/env bash
# LIVE LOG BRIDGE (file-based, self-contained, no external service).
# Background daemon: every $INTERVAL sec, snapshot the job's full output (step stdout + pier/agent
# output) and FORCE-PUSH it to a throwaway branch  gha-live-logs/<task>  so Claude Code can
# `git fetch` + read it MID-RUN. One branch per matrix task = no parallel-push collisions.
# Force-push keeps the branch at a single ever-replaced commit (bounded churn, code branches clean).
#
# Args: $1=task  $2=step-stdout log (abs)  $3=pier log (abs, trial_output.log)  $4=interval(default 15)
# Optional env: GT_CAMPAIGN_FEATURE_LIVE / FEATURE_LIVE_LOG — path to gt_campaign_feature_live.jsonl
# Best-effort: every failure is swallowed; this NEVER fails the job.
set +e
TASK="${1:?task}"; STEPLOG="${2:-/dev/null}"; PIERLOG="${3:-/dev/null}"; INTERVAL="${4:-15}"
REPO="${GITHUB_REPOSITORY:-}"; TOKEN="${GH_LIVE_TOKEN:-${GITHUB_TOKEN:-}}"   # GHA does NOT auto-export GITHUB_TOKEN; caller passes GH_LIVE_TOKEN
[ -z "$REPO" ] || [ -z "$TOKEN" ] && { echo "[live-log] no repo/token — disabled"; exit 0; }
BRANCH="gha-live-logs/${TASK}"
URL="https://x-access-token:${TOKEN}@github.com/${REPO}.git"
FEATURE_LIVE="${FEATURE_LIVE_LOG:-${GT_CAMPAIGN_FEATURE_LIVE:-}}"
# Common DeepSWE / pier locations when env is unset.
if [ -z "$FEATURE_LIVE" ] || [ ! -f "$FEATURE_LIVE" ]; then
  for cand in \
      "${GT_C_OUT:-}/gt_campaign_feature_live.jsonl" \
      "/tmp/gt_out/gt_campaign_feature_live.jsonl" \
      "$(dirname "$PIERLOG")/gt_out/gt_campaign_feature_live.jsonl" \
      "$(dirname "$PIERLOG")/gt_campaign_feature_live.jsonl"; do
    if [ -n "$cand" ] && [ -f "$cand" ]; then FEATURE_LIVE="$cand"; break; fi
  done
fi
W="$(mktemp -d)"
( cd "$W" && git init -q && git config user.email gha@live && git config user.name gha-live \
    && git checkout -qb live ) 2>/dev/null || exit 0
echo "[live-log] daemon up: branch=$BRANCH every ${INTERVAL}s (read it with: git fetch origin $BRANCH && git show FETCH_HEAD:live.log)"
N=0
while true; do
  N=$((N+1))
  {
    echo "=== LIVE SNAPSHOT #$N | $(date -u +%FT%TZ) | task=$TASK ==="
    # KEY EVENTS first (grep the FULL logs, not a tail) so the once-emitted env-start/slim/build
    # lines are never drowned by the per-8s RESMON flood that fills any tail window.
    echo "--- KEY EVENTS (full logs, non-RESMON) ---"
    grep -aE 'DOCKERFILE SLIM|SLIM-VERIFY|ENV-START|gt-shim|FROM |go build|go test|go mod|npm |cargo |pnpm |buildx|bake|Pulling fs|naming to|CACHED|exporting|writing image|container UP|exited early|EXCEPTION|Traceback|Error response' "$STEPLOG" "$PIERLOG" 2>/dev/null | tail -50
    echo "--- mem trend (last 6) ---"
    grep -aE 'MEMDIAG.*MemFree|containers=' "$STEPLOG" 2>/dev/null | tail -6
    echo "--- pier tail (last 15, non-RESMON) ---"
    grep -aviE 'RESMON|MEMDIAG|RESTOP' "$PIERLOG" 2>/dev/null | tail -15
    echo "--- step tail (last 8) ---"
    tail -8 "$STEPLOG" 2>/dev/null
    echo "--- FEATURE LIVE (DELIVERED|HOLD|SUPPRESSED|ERROR|NOT_ELIGIBLE only) ---"
    if [ -n "$FEATURE_LIVE" ] && [ -f "$FEATURE_LIVE" ]; then
      # Counts by stage then last 40 lines — mid-run rising DELIVERED is the signal.
      python3 - "$FEATURE_LIVE" <<'PY' 2>/dev/null || tail -40 "$FEATURE_LIVE"
import json, sys, collections
path = sys.argv[1]
counts = collections.Counter()
n = 0
with open(path, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        n += 1
        try:
            row = json.loads(line)
        except Exception:
            continue
        counts[str(row.get("stage") or "?")] += 1
print(f"lines={n} stages={dict(counts)}")
PY
      echo "--- feature_live tail ---"
      tail -40 "$FEATURE_LIVE"
    else
      echo "(no gt_campaign_feature_live.jsonl yet)"
    fi
  } > "$W/live.log"
  # Also publish a dedicated feature_live.log sibling when present (read_live.sh).
  if [ -n "$FEATURE_LIVE" ] && [ -f "$FEATURE_LIVE" ]; then
    cp -f "$FEATURE_LIVE" "$W/feature_live.log" 2>/dev/null || true
  else
    printf '(no feature live yet)\n' > "$W/feature_live.log"
  fi
  ( cd "$W" \
      && cp /dev/null .keep 2>/dev/null \
      && git add -A \
      && git commit -qm "live #$N $(date -u +%T)" --allow-empty \
      && git push -qf "$URL" live:"$BRANCH" ) 2>/dev/null || true
  sleep "$INTERVAL"
done
