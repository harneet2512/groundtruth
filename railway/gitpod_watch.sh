#!/usr/bin/env bash
###############################################################################
# gitpod_watch.sh — clean, live play-by-play of an in-progress OH+GT run.
#
# Run in a SECOND Gitpod terminal WHILE `bash railway/gitpod_run.sh` runs in the
# first. It tails the run log and surfaces ONLY the lines that matter so you can
# pick up a mistake the moment it happens, instead of drowning in the firehose:
#
#   §   bootstrap step header (=== [N] ... ===)   — progress
#   GT▸ a GroundTruth delivery/trace to the agent  — what GT sent
#   ▸   an agent action (read / edit / run / finish) — what the model did
#   $   an LLM cost/call line                        — spend ticking
#   ‼   an error / traceback / OOM / no-space        — the mistake, live
#   ⚠   a warning
#   ·   resolution / patch / post-run summary
#
# Ctrl-C stops WATCHING only — it does not stop the run.
###############################################################################
set -uo pipefail
LOG="${1:-/tmp/gt_debug/full_run.log}"

echo "⏳ waiting for run log: $LOG  (start the run first: bash railway/gitpod_run.sh)"
for _ in $(seq 1 120); do [ -s "$LOG" ] && break; sleep 2; done
if [ ! -s "$LOG" ]; then
  echo "✗ no run log at $LOG after 4 min — is a run actually going?"
  exit 1
fi

echo "▶ watching $LOG"
echo "  legend:  §step  GT▸delivery  ▸agent  \$cost  ‼error  ⚠warn  ·result   (Ctrl-C = stop watching only)"
echo "──────────────────────────────────────────────────────────────────────"
tail -n +1 -F "$LOG" 2>/dev/null | awk '
  /Traceback|[Ee]rror:|FATAL|[Ee]xception|no space left|Killed|OOMKilled|Cannot connect to the Docker/ { print "‼  " $0; fflush(); next }
  /WARN|warning:/                                                                   { print "⚠  " $0; fflush(); next }
  /=== \[[0-9]/                                                                      { print "§  " $0; fflush(); next }
  /\[GT_COST|llm_call|LLM call|tokens/                                              { print "$  " $0; fflush(); next }
  /<gt-task-brief|<gt-evidence|<gt-graph-map|GT_META|\[GT_TRACE|\[GT_STATUS|GT_DELIVERY|\[CONTRACT\]|Called by:/ { print "GT> " $0; fflush(); next }
  /CmdRunAction|FileEditAction|FileReadAction|IPythonRunCellAction|MessageAction|AgentFinishAction|"action":|THOUGHT|COMMAND:/ { print ">  " $0; fflush(); next }
  /resolved|RESOLVED|FAIL_TO_PASS|PASS_TO_PASS|POST-RUN|patch applied|git diff/      { print "·  " $0; fflush(); next }
'
