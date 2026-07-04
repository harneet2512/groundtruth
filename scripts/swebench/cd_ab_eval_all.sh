#!/bin/bash
# Auto-run SWE-bench harness eval on each lane as its tmux session ends.
# Runs evals sequentially so they don't oversubscribe the box. Emits
# resolved counts into /tmp/cd_ab_resolved.env for downstream use.
set -u
RESOLVED_FILE=/tmp/cd_ab_resolved.env
: > "$RESOLVED_FILE"
done_lanes=""

lane_done() { [[ ",$done_lanes," == *",$1,"* ]]; }

run_eval() {
  local lane="$1" outdir="$2"
  # RC-17 (F-009): timestamp + 6-char random hex suffix. Two parallel
  # launches in the same wall-clock second no longer collide on run_id.
  # Falls back to /dev/urandom if `python3 -c secrets` is unavailable.
  local _hex
  _hex=$(python3 -c 'import secrets; print(secrets.token_hex(3))' 2>/dev/null \
         || head -c 3 /dev/urandom | od -An -vtx1 | tr -d ' \n')
  local run_id="cd_ab_${lane}_$(date +%s)_${_hex}"
  echo "[$(date +%H:%M:%S)] starting eval: $lane ($outdir) run_id=$run_id"
  bash ~/eval_wave_preds.sh "$outdir" "$run_id" > "/tmp/eval_${lane}.log" 2>&1 || {
    echo "[$(date +%H:%M:%S)] eval FAILED for $lane"
    return 1
  }
  # Resolved count lives in /tmp/${run_id}/results.json or report
  local resolved
  resolved=$(grep -oE '"resolved":\s*\[[^]]*\]' "/tmp/${run_id}/"*report*.json 2>/dev/null \
             | head -1 | python3 -c 'import sys,json,re; s=sys.stdin.read(); m=re.search(r"\[.*\]",s); print(0 if not m else len(json.loads(m.group(0))))' 2>/dev/null || echo "-1")
  if [ -z "$resolved" ] || [ "$resolved" = "" ]; then
    # Fallback: scrape log
    resolved=$(grep -oE 'resolved.*:\s*[0-9]+' "/tmp/eval_${lane}.log" | tail -1 | grep -oE '[0-9]+' | head -1)
    resolved="${resolved:--1}"
  fi
  echo "RESOLVED_${lane}=${resolved}" >> "$RESOLVED_FILE"
  echo "[$(date +%H:%M:%S)] done eval: $lane resolved=$resolved"
}

while :; do
  sessions=$(tmux ls 2>/dev/null | grep -oE 'lane_[AB]_(nolsp|lsp)' || true)
  # A lane is "done" iff (a) its tmux session is gone AND (b) its preds.json exists
  for lane_dir in \
      "a_nolsp:/tmp/phase_a_nolsp/repeat_1" \
      "a_lsp:/tmp/phase_a_lsp/repeat_1" \
      "b_nolsp:/tmp/phase_b_nolsp/repeat_1" \
      "b_lsp:/tmp/phase_b_lsp/repeat_1"; do
    lane="${lane_dir%%:*}"
    outdir="${lane_dir##*:}"
    lane_done "$lane" && continue
    tmux_name="lane_${lane^^}"
    # Translate a_nolsp → lane_A_nolsp
    tmux_name="lane_$(echo "$lane" | awk -F_ '{print toupper($1) "_" $2}')"
    echo "$sessions" | grep -q "^${tmux_name}$" && continue  # still running
    [ -f "$outdir/preds.json" ] || continue  # not yet
    done_lanes="${done_lanes},${lane}"
    run_eval "$lane" "$outdir" &
  done
  # Exit when all 4 evals kicked off
  [ "$(echo "$done_lanes" | tr , '\n' | grep -c .)" -ge 4 ] && break
  sleep 30
done
wait
echo "[$(date +%H:%M:%S)] all evals complete"
cat "$RESOLVED_FILE"
