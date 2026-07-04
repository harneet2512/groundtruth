#!/bin/bash
# 4-lane watcher for the C+D strict-on/off A/B.
# Exits when all tmux lane_* sessions end.
set -u
log() { echo "[$(date +%H:%M:%S)] $*"; }
start=$(date +%s)
while :; do
  now=$(date +%s); elapsed=$(( (now - start) / 60 ))
  sessions=$(tmux ls 2>/dev/null | grep -cE 'lane_[AB]_' || true)
  containers=$(docker ps --format '{{.Names}}' 2>/dev/null | wc -l)
  a_n=$(find /tmp/phase_a_nolsp/repeat_1 -maxdepth 2 -name preds.json 2>/dev/null | wc -l)
  a_l=$(find /tmp/phase_a_lsp/repeat_1   -maxdepth 2 -name preds.json 2>/dev/null | wc -l)
  b_n=$(find /tmp/phase_b_nolsp/repeat_1 -maxdepth 2 -name preds.json 2>/dev/null | wc -l)
  b_l=$(find /tmp/phase_b_lsp/repeat_1   -maxdepth 2 -name preds.json 2>/dev/null | wc -l)
  kn=$(cat /tmp/phase_a_nolsp/repeat_1/killed_tasks.jsonl /tmp/phase_a_lsp/repeat_1/killed_tasks.jsonl \
           /tmp/phase_b_nolsp/repeat_1/killed_tasks.jsonl /tmp/phase_b_lsp/repeat_1/killed_tasks.jsonl 2>/dev/null | wc -l)
  log "elapsed=${elapsed}m containers=$containers sessions=$sessions | A_n=$a_n/10 A_l=$a_l/10 B_n=$b_n/10 B_l=$b_l/10 | kills=$kn"
  if [ "$sessions" = "0" ]; then
    log "all 4 lanes complete"
    break
  fi
  sleep 30
done
