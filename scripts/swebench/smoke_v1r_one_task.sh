#!/usr/bin/env bash
# Single-task cloud smoke for V1R-map.
#
# Runs ONE SWE-bench-Live task (default: python-babel__babel-1141) with the
# V1R-map arm only, then asserts on the resulting log that:
#   1. graph.db was built or pre-existed (V3IDX marker)
#   2. sentence_transformers is present (ST marker)
#   3. the bundle was launched (script injected marker)
#   4. the brief was non-empty (PREPENDED marker, char count >= floor)
#
# Cost surface: ~$0.32 per run (qwen3-coder Vertex MaaS, ground-truthed).
# Wall clock: ~5-8 min (3 min indexer/pip first run, ~4 min agent loop).
#
# Usage:
#   bash scripts/swebench/smoke_v1r_one_task.sh
#   SMOKE_TASK_ID=keras-team__keras-20389 bash scripts/swebench/smoke_v1r_one_task.sh
#
# Env knobs:
#   SMOKE_TASK_ID      — instance_id (default: python-babel__babel-1141)
#   SMOKE_BRIEF_FLOOR  — minimum brief char count for PASS (default: 500)
#   V1R_EXP_DIR        — scratch dir (default: ~/v1r_experiment)
#   GT_LOG_DIR         — where briefs persist (default: ~/v1r_experiment/smoke_briefs)
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAUNCHER="$REPO_DIR/scripts/swebench/run_all_v1r_arms_vertex.sh"

TASK_ID="${SMOKE_TASK_ID:-python-babel__babel-1141}"
FLOOR="${SMOKE_BRIEF_FLOOR:-500}"
EXP_DIR="${V1R_EXP_DIR:-$HOME/v1r_experiment}"
LOG="$EXP_DIR/arm_V1R-map.log"

export GT_LOG_DIR="${GT_LOG_DIR:-$EXP_DIR/smoke_briefs}"
mkdir -p "$GT_LOG_DIR"

echo "=== V1R-map single-task smoke ==="
echo "  TASK_ID=$TASK_ID"
echo "  BRIEF_FLOOR=$FLOOR"
echo "  GT_LOG_DIR=$GT_LOG_DIR"
echo "  COST: ~\$0.32 (qwen3-coder Vertex MaaS) — confirm before launching"
echo ""

SINGLE_ARM=V1R-map V1R_TASK_FILTER="$TASK_ID" bash "$LAUNCHER"
rc=$?
if [ $rc -ne 0 ]; then
  echo "FAIL: launcher exited $rc"
  exit 1
fi

echo ""
echo "=== Asserting on log: $LOG ==="
if [ ! -f "$LOG" ]; then
  echo "FAIL: log $LOG missing"
  exit 1
fi

pass=1

# Gate 1: graph.db
if grep -qE "(GT graph\.db built|already exists, skipping rebuild|V3IDX_INDEX_BUILT)" "$LOG"; then
  echo "  [PASS] graph.db built or pre-existed"
else
  echo "  [FAIL] graph.db marker not found"
  pass=0
fi

# Gate 2: sentence_transformers
if grep -qE "(sentence_transformers installed|sentence_transformers already present|ST_INSTALLED)" "$LOG"; then
  echo "  [PASS] sentence_transformers present"
else
  echo "  [FAIL] sentence_transformers marker not found"
  pass=0
fi

# Gate 3: bundle launched
if grep -q "GT pretask brief script injected" "$LOG"; then
  echo "  [PASS] bundle launched"
else
  echo "  [FAIL] 'GT pretask brief script injected' not found"
  pass=0
fi

# Gate 4: brief non-empty + length floor
prepend_line=$(grep -E "GT pretask brief PREPENDED" "$LOG" | tail -1 || true)
if [ -z "$prepend_line" ]; then
  echo "  [FAIL] 'GT pretask brief PREPENDED' not found"
  if grep -q "empty/no signal" "$LOG"; then
    echo "         (saw 'empty/no signal' — brief generator returned no candidates)"
  fi
  pass=0
else
  chars=$(echo "$prepend_line" | grep -oE "[0-9]+ chars" | head -1 | grep -oE "[0-9]+" || echo "0")
  if [ "$chars" -ge "$FLOOR" ]; then
    echo "  [PASS] brief PREPENDED ${chars} chars (floor=${FLOOR})"
  else
    echo "  [FAIL] brief PREPENDED ${chars} chars < floor=${FLOOR}"
    pass=0
  fi
fi

# Gate 5 (informational): persisted brief in GT_LOG_DIR
brief_file="$GT_LOG_DIR/${TASK_ID}_brief.txt"
if [ -f "$brief_file" ]; then
  size=$(wc -c < "$brief_file")
  echo "  [INFO] persisted brief: $brief_file ($size bytes)"
fi

echo ""
if [ $pass -eq 1 ]; then
  echo "=== V1R-map smoke: PASS ==="
  exit 0
else
  echo "=== V1R-map smoke: FAIL ==="
  echo "Inspect: $LOG"
  exit 1
fi
