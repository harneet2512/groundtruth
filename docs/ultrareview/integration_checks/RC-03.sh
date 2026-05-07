#!/usr/bin/env bash
# RC-03 integration check — per-task isolation under concurrency.
#
# WHAT IT PROVES
#   With the RC-03 fix in scripts/swebench/gt_track4_pre_run.py
#   (per-thread instance_id resolution + threading.Lock around _pending +
#   weakref safety net), a 6-task batch run at 4-way concurrency must
#   produce per-task gt_layers.log files where each file has exactly ONE
#   canonical line carrying the matching instance_id, with zero
#   gate_verdict=unresolved and zero gate_verdict=no_close_wrap artifacts.
#   Cross-checks: each task's gt_pre_finish_gate.json `result` field
#   matches the L5 cell on the corresponding gt_layers.log line.
#
#   Empirical baseline before RC-03 (5-task, 5-way smoke at
#   .tmp_validation_5task/validation_5task_1778091091/): 4/5 tasks emit
#   wrong contract lines (2x unresolved, 2x no_close_wrap).
#   Target after RC-03: 0/6 wrong contract lines.
#
# DO NOT RUN THIS SCRIPT ON A VM AS PART OF AUTOMATED CI.
#   It launches a real 6-task SWE-agent batch and costs LLM tokens. Treat
#   as a manual diagnostic invoked by the operator after a code change in
#   gt_track4_pre_run.py affecting hook concurrency, _pending mutations,
#   instance_id resolution, or env.close wrapping.
#
# USAGE
#   bash RC-03.sh                          # uses defaults below
#   GT_INDEXES_ROOT=/data/eval_indexes \
#   GT_TRACK4_LOG_DIR=/tmp/rc03_check \
#   bash RC-03.sh
#
# DEPENDENCIES
#   - python3 with sweagent installed and reachable (this is a VM-side
#     check; the local Windows worktree does not have sweagent).
#   - jq for JSON cross-checks.
#   - Per-instance graph.db's prebuilt under
#     $GT_INDEXES_ROOT/<task_id>/graph.db for each TASK_ID below.
#   - litellm proxy reachable at $LITELLM_BASE_URL (default
#     http://127.0.0.1:4000).
#
# EXIT CODES
#   0 — every per-task gt_layers.log has exactly one canonical line, zero
#       unresolved, zero no_close_wrap, and L5 cells agree with
#       gt_pre_finish_gate.json `result` fields where the gate JSON
#       exists.
#   1 — at least one task has a wrong contract line OR L5/result
#       disagreement.
#   2 — preflight failure (missing dependency / index / config).

set -u
set -o pipefail

# ----- defaults --------------------------------------------------------
: "${GT_INDEXES_ROOT:?GT_INDEXES_ROOT must be set to a dir containing per-instance graph.db}"
: "${GT_TRACK4_LOG_DIR:=/tmp/rc03_check_$$}"
: "${LITELLM_BASE_URL:=http://127.0.0.1:4000}"
: "${NUM_WORKERS:=4}"
: "${RUN_OUTPUT_DIR:=$GT_TRACK4_LOG_DIR/run}"

# 6 tasks across 3 different repos to stress per-thread isolation. These
# IDs are intentionally generic — operator must ensure each has a
# prebuilt graph.db under $GT_INDEXES_ROOT/<id>/graph.db.
TASK_IDS=(
  "aws-cloudformation__cfn-lint-3855"
  "aws-cloudformation__cfn-lint-3862"
  "aws-cloudformation__cfn-lint-3875"
  "aws-cloudformation__cfn-lint-3890"
  "amoffat__sh-744"
  "pdm-project__pdm-2789"
)

# ----- preflight -------------------------------------------------------
command -v python3 >/dev/null 2>&1 || { echo "FATAL: python3 not found"; exit 2; }
command -v jq      >/dev/null 2>&1 || { echo "FATAL: jq not found";      exit 2; }
python3 -c "import sweagent" >/dev/null 2>&1 || {
  echo "FATAL: python3 cannot import sweagent — run from a sweagent venv";
  exit 2;
}
for tid in "${TASK_IDS[@]}"; do
  test -f "$GT_INDEXES_ROOT/$tid/graph.db" || {
    echo "FATAL: missing $GT_INDEXES_ROOT/$tid/graph.db"; exit 2;
  }
done

mkdir -p "$RUN_OUTPUT_DIR"
echo "[RC-03] launching 6-task batch at concurrency=$NUM_WORKERS"
echo "[RC-03] log dir: $GT_TRACK4_LOG_DIR"
echo "[RC-03] output : $RUN_OUTPUT_DIR"

# ----- launch ----------------------------------------------------------
# Operator-runnable; the exact swe_agent_smoke_runner.py invocation
# depends on which model + budget the operator wants to spend on.
# We surface the canonical command and let the operator approve. The
# smoke runner is responsible for exporting GT_INDEXES_ROOT (RC-07 fix)
# and registering the GTTrack4PreRunHook.
cat <<EOF
[RC-03] OPERATOR ACTION REQUIRED — copy and run:

  GT_INDEXES_ROOT=$GT_INDEXES_ROOT \\
  GT_TRACK4_LOG_DIR=$GT_TRACK4_LOG_DIR \\
  python3 scripts/swebench/swe_agent_smoke_runner.py \\
    --tasks "${TASK_IDS[*]}" \\
    --num-workers $NUM_WORKERS \\
    --output-dir $RUN_OUTPUT_DIR \\
    --litellm-base-url $LITELLM_BASE_URL

Once the run finishes, re-run this script with VERIFY_ONLY=1 to score it:

  VERIFY_ONLY=1 \\
  GT_INDEXES_ROOT=$GT_INDEXES_ROOT \\
  GT_TRACK4_LOG_DIR=$GT_TRACK4_LOG_DIR \\
  bash $0
EOF

if [[ "${VERIFY_ONLY:-0}" != "1" ]]; then
  exit 0
fi

# ----- verify ----------------------------------------------------------
fail=0
total=${#TASK_IDS[@]}
for tid in "${TASK_IDS[@]}"; do
  log="$GT_TRACK4_LOG_DIR/$tid/gt_layers.log"
  gate="$GT_TRACK4_LOG_DIR/$tid/gt_pre_finish_gate.json"

  if ! test -f "$log"; then
    echo "FAIL $tid: gt_layers.log missing"
    fail=$((fail + 1))
    continue
  fi

  # Every per-task log must contain exactly one canonical L3..L6 line.
  canonical_count=$(grep -cE "^task=$tid +L3_edits=" "$log" || true)
  if [[ "$canonical_count" != "1" ]]; then
    echo "FAIL $tid: expected 1 canonical line, got $canonical_count"
    fail=$((fail + 1))
    continue
  fi

  line=$(grep -E "^task=$tid +L3_edits=" "$log")

  # Forbidden artifacts.
  if grep -q "L5_gate=unresolved" <<<"$line"; then
    echo "FAIL $tid: L5_gate=unresolved (RC-03 corruption guard tripped)"
    fail=$((fail + 1))
    continue
  fi
  if grep -q "L5_gate=no_close_wrap" <<<"$line"; then
    echo "FAIL $tid: L5_gate=no_close_wrap (close-wrap never fired)"
    fail=$((fail + 1))
    continue
  fi

  # Cross-check L5 cell vs gt_pre_finish_gate.json `result`.
  l5=$(sed -nE 's/.* L5_gate=([^ ]+) .*/\1/p' <<<"$line")
  if test -f "$gate"; then
    json_result=$(jq -r '.result // "unknown"' "$gate" 2>/dev/null || echo "parse_err")
    # Allow autosubmit override (close-wrap maps absent/no_close_wrap →
    # autosubmit when exit_status is autosubmit-shaped).
    if [[ "$l5" != "$json_result" && "$l5" != "autosubmit" ]]; then
      echo "FAIL $tid: L5_gate=$l5 disagrees with gt_pre_finish_gate.json result=$json_result"
      fail=$((fail + 1))
      continue
    fi
  fi
  echo "PASS $tid: L5_gate=$l5"
done

echo
if [[ "$fail" -eq 0 ]]; then
  echo "[RC-03] PASS — all $total tasks emit exactly one correct contract line"
  exit 0
else
  echo "[RC-03] FAIL — $fail / $total tasks have wrong contract lines"
  exit 1
fi
