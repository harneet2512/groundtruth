#!/usr/bin/env bash
# Durably save a GHA run: ALL artifacts + the full job log + a manifest, filed by
# timestamp under .claude/reports/runs/, and appended to RUN_LEDGER.md.
# Nothing about a run is ever lost. Usage: scripts/save_gha_run.sh <run_id> [label]
set -uo pipefail
REPO="harneet2512/groundtruth"
RUN_ID="${1:?usage: save_gha_run.sh <run_id> [label]}"
LABEL="${2:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="$ROOT/.claude/reports/runs"
LEDGER="$ROOT/RUN_LEDGER.md"

# Run metadata
meta=$(gh run view "$RUN_ID" -R "$REPO" --json databaseId,name,headBranch,headSha,status,conclusion,createdAt,displayTitle,workflowName 2>/dev/null)
[ -z "$meta" ] && { echo "FATAL: cannot fetch run $RUN_ID"; exit 1; }
get(){ echo "$meta" | python -c "import sys,json;print(json.load(sys.stdin).get('$1','') or '')"; }
WF=$(get workflowName); BR=$(get headBranch); SHA=$(get headSha); ST=$(get status); CC=$(get conclusion); CREATED=$(get createdAt); TITLE=$(get displayTitle)
TS=$(echo "$CREATED" | tr -d ':-' | tr 'T' '_' | cut -c1-15)  # 20260603_145501
SAFEWF=$(echo "$WF" | tr ' /:' '___' | tr -cd '[:alnum:]_-')
DEST="$ARCHIVE/${TS}__${SAFEWF}__${RUN_ID}${LABEL:+__$LABEL}"
mkdir -p "$DEST"

echo "=== saving run $RUN_ID ($WF) -> $DEST ==="
# 1) full job log (always available even if no artifacts)
gh run view "$RUN_ID" -R "$REPO" --log > "$DEST/full_job.log" 2>&1 || \
  gh run view "$RUN_ID" -R "$REPO" --log-failed > "$DEST/full_job.log" 2>&1 || echo "(log unavailable — run in progress?)"
# 2) every artifact
gh run download "$RUN_ID" -R "$REPO" -D "$DEST/artifacts" 2>&1 | tail -3 || echo "(no artifacts yet)"

# 3) extract the signals that matter for the benchmark team
BRIEF=$(find "$DEST/artifacts" -name 'delivered_instruction.txt' -o -name 'output.jsonl' 2>/dev/null | head -1)
REACHED="?"; grep -qaE "<gt-task-brief>" "$DEST/artifacts"/*/*delivered_instruction.txt 2>/dev/null && REACHED="YES"
RESULT="?"
# pier reward / OH resolved
REWARD=$(grep -aoE "Reward[^0-9]*[0-9.]+" "$DEST/full_job.log" 2>/dev/null | grep -oE "[0-9.]+" | head -1)
RESOLVED=$(grep -aoE "RESOLVED=[0-9]+" "$DEST/full_job.log" 2>/dev/null | grep -oE "[0-9]+" | head -1)
[ -n "${REWARD:-}" ] && RESULT="reward=$REWARD"
[ -n "${RESOLVED:-}" ] && RESULT="resolved=$RESOLVED"
# GT hook firing histogram across the trajectory (the WHOLE architecture, not just brief)
echo "=== GT hook firings (across trajectory) ===" > "$DEST/hook_firings.txt"
TRAJ=$(find "$DEST/artifacts" -name 'result.json' -o -name 'output.jsonl' 2>/dev/null)
for f in $TRAJ "$DEST/full_job.log"; do [ -f "$f" ] && grep -aoiE "gt_hook|gt understand|gt verify|<gt-evidence>|<gt-task-brief>|behavioral_contract|post_edit|post_view|CONSENSUS|L3_router_v2|test_assertions" "$f" 2>/dev/null; done | sort | uniq -c | sort -rn >> "$DEST/hook_firings.txt"

# 4) manifest
cat > "$DEST/MANIFEST.md" <<EOF
# Run $RUN_ID — $WF
- saved_at_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)
- workflow: $WF
- title: $TITLE
- branch: $BR
- commit: $SHA
- created: $CREATED
- status/conclusion: $ST / $CC
- brief_reached_agent: $REACHED
- result: $RESULT
- files:
$(cd "$DEST" && find . -type f | sed 's/^/  - /')
EOF

# 5) ledger append (create header if missing)
[ -f "$LEDGER" ] || printf '# RUN_LEDGER — every run, saved durably\n\n| saved_utc | run_id | workflow | task/title | commit | status | conclusion | brief_reached | result | archive |\n|---|---|---|---|---|---|---|---|---|---|\n' > "$LEDGER"
printf '| %s | [%s](https://github.com/%s/actions/runs/%s) | %s | %s | %s | %s | %s | %s | %s | `%s` |\n' \
  "$(date -u +%Y-%m-%dT%H:%MZ)" "$RUN_ID" "$REPO" "$RUN_ID" "$WF" "${TITLE//|/}" "${SHA:0:8}" "$ST" "$CC" "$REACHED" "${RESULT//|/}" "${DEST#$ROOT/}" >> "$LEDGER"

echo "SAVED -> ${DEST#$ROOT/}"
echo "--- hook firings ---"; cat "$DEST/hook_firings.txt"
