#!/bin/bash
# Monitor foundation v2 experiment progress.
# Usage: bash monitor_fv2.sh

RESULTS_DIR="$HOME/foundation_v2"
LOG="$RESULTS_DIR/monitoring_log.txt"

TS=$(date '+%Y-%m-%d %H:%M:%S UTC')

echo "=== Experiment Status at $TS ===" | tee -a "$LOG"

# Check if experiment is still running
if pgrep -f "run_fv2_100" > /dev/null 2>&1; then
    echo "Experiment: RUNNING" | tee -a "$LOG"
else
    echo "Experiment: NOT RUNNING (finished or crashed)" | tee -a "$LOG"
fi

# Check each condition
for cond in condition_a condition_b; do
    output_file=$(find "$RESULTS_DIR/$cond" -name "output.jsonl" 2>/dev/null | head -1)
    if [ -n "$output_file" ] && [ -f "$output_file" ]; then
        count=$(wc -l < "$output_file")
        echo "$cond: $count/100 tasks completed" | tee -a "$LOG"
    else
        echo "$cond: not started yet" | tee -a "$LOG"
    fi
done

# Disk usage
DISK=$(df -h / | tail -1 | awk '{print $3 " used, " $4 " free (" $5 ")"}')
echo "Disk: $DISK" | tee -a "$LOG"

# Docker containers
RUNNING=$(docker ps -q 2>/dev/null | wc -l)
echo "Docker containers running: $RUNNING" | tee -a "$LOG"

# Last log line
echo "Last experiment log:" | tee -a "$LOG"
tail -1 "$RESULTS_DIR/experiment.log" 2>/dev/null | head -c 200 | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "---" | tee -a "$LOG"
