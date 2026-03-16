#!/bin/bash
# Pull latest benchmark stats from VM and update benchmark_update.md
# Usage: bash scripts/pull_benchmark_stats.sh

VM=34.63.189.29
KEY=~/.ssh/google_compute_engine

echo "Pulling stats from VM..."
STATS=$(ssh -i $KEY $VM "cat ~/groundtruth/logs/latest_stats.json 2>/dev/null")
HOURLY=$(ssh -i $KEY $VM "cat ~/groundtruth/logs/hourly_stats.log 2>/dev/null")

echo "Latest stats:"
echo "$STATS" | python3 -m json.tool 2>/dev/null || echo "$STATS"
echo ""
echo "Hourly log:"
echo "$HOURLY"
