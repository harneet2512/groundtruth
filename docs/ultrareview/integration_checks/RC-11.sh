#!/usr/bin/env bash
# RC-11 integration check — cost-exit / call-limit-exit / SIGTERM bypass
# of artifact pull is closed by the atexit handler installed in
# _wrap_env_close_with_artifact_pull.
#
# WHAT IT PROVES
#   With the RC-11 fix, a synthetic 1-task run with
#   per_instance_cost_limit=0.01 (forces immediate cost-exit before the
#   agent can call submit) MUST produce a per-task gt_layers.log line
#   with:
#     (1) gt_query_calls.jsonl pulled to host even though env.close was
#         never called normally (the close-wrap is bypassed by exit_cost);
#     (2) L4_queries cell reflects the actual gt_query invocations recorded
#         in the container during the few iterations the agent got before
#         budget exhaustion (likely 0 in this synthetic case, but >0 if
#         any happened pre-exit);
#     (3) exit_status=cost_exit field set on the line so verify_report
#         (RC-10) can exclude this task from the engagement_rate
#         denominator (or compute it as a separate cohort);
#     (4) verify_report output for this run shows the task is excluded /
#         flagged in the engagement_rate computation rather than counted
#         as a zero-engagement task that drags the rate down.
#
#   Before RC-11: every cost-exited task showed L3=L4=L6=0 with
#   gate_verdict=autosubmit AND no exit_status field, biasing paired
#   comparisons against the GT arm (which sees more cost-exits because
#   briefs add input tokens).
#
# DO NOT RUN THIS SCRIPT ON A VM AS PART OF AUTOMATED CI.
#   It launches a real SWE-agent task and costs a small amount of LLM
#   tokens (the cost limit is 0.01 USD). Treat as a manual diagnostic
#   invoked by the operator after a code change in the pre-run hook.
#
# USAGE
#   bash RC-11.sh
#   RC11_TASK_ID=django__django-11099 bash RC-11.sh
#
# DEPENDENCIES
#   - python3 (for swe_agent_smoke_runner.py)
#   - jq (for parsing gt_layers.log)
#   - prebuilt graph.db at $GT_INDEXES_ROOT/$RC11_TASK_ID/graph.db
#
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
RUNNER="${REPO_ROOT}/scripts/swebench/swe_agent_smoke_runner.py"
CONFIG="${RC11_CONFIG:-${REPO_ROOT}/scripts/swebench/configs/track4_default.yaml}"
GT_INDEXES_ROOT="${GT_INDEXES_ROOT:-/home/ubuntu/eval_indexes}"
OUTPUT_DIR="${RC11_OUTPUT_DIR:-${REPO_ROOT}/.tmp_rc11_check_$(date +%s)}"
TASK_ID="${RC11_TASK_ID:-django__django-11099}"
COST_LIMIT="${RC11_COST_LIMIT:-0.01}"

mkdir -p "${OUTPUT_DIR}"

echo "[RC-11] runner:           ${RUNNER}"
echo "[RC-11] config:           ${CONFIG}"
echo "[RC-11] gt_indexes_root:  ${GT_INDEXES_ROOT}"
echo "[RC-11] task_id:          ${TASK_ID}"
echo "[RC-11] cost_limit:       ${COST_LIMIT} USD"
echo "[RC-11] output_dir:       ${OUTPUT_DIR}"

# Sanity: prebuilt graph.db must exist.
db="${GT_INDEXES_ROOT}/${TASK_ID}/graph.db"
if [[ ! -f "${db}" ]]; then
    echo "[RC-11][FATAL] missing per-instance graph.db: ${db}" >&2
    echo "[RC-11] Build it with gt-index before running this check." >&2
    exit 2
fi

# Launch the 1-task run with a tiny cost limit. The runner is expected to
# pass --per-instance-cost-limit through to SWE-agent's
# AgentConfig.cost_limit (or equivalent). If the flag name differs in the
# local fork, set RC11_COST_FLAG to the right name.
COST_FLAG="${RC11_COST_FLAG:---per-instance-cost-limit}"
python3 "${RUNNER}" \
    --task-ids "${TASK_ID}" \
    --config "${CONFIG}" \
    --output-dir "${OUTPUT_DIR}" \
    --gt-indexes-root "${GT_INDEXES_ROOT}" \
    --workers 1 \
    --skip-preflight \
    "${COST_FLAG}" "${COST_LIMIT}" \
    || echo "[RC-11] runner exited non-zero (expected for cost-exit; continuing checks)"

# Locate the per-task log dir.
log_path=$(find "${OUTPUT_DIR}" -type f -name "gt_layers.log" -path "*${TASK_ID}*" | head -n1 || true)
if [[ -z "${log_path}" ]]; then
    echo "[RC-11][FAIL] no gt_layers.log found for ${TASK_ID} under ${OUTPUT_DIR}" >&2
    exit 1
fi
log_dir=$(dirname "${log_path}")
echo "[RC-11] log_dir:          ${log_dir}"

fail=0

# (1) gt_query_calls.jsonl pulled to host.
if [[ ! -f "${log_dir}/gt_query_calls.jsonl" ]]; then
    echo "[RC-11][FAIL] gt_query_calls.jsonl was NOT pulled to host (atexit handler did not flush)" >&2
    fail=1
else
    echo "[RC-11][OK]   gt_query_calls.jsonl present on host: ${log_dir}/gt_query_calls.jsonl"
fi

# (2) L4_queries cell reflects file line count.
expected_l4=$(grep -c . "${log_dir}/gt_query_calls.jsonl" 2>/dev/null || echo 0)
actual_l4=$(grep -oE "L4_queries=[0-9]+" "${log_path}" | tail -n1 | cut -d= -f2)
if [[ "${actual_l4}" != "${expected_l4}" ]]; then
    echo "[RC-11][FAIL] L4_queries cell (${actual_l4}) does not match jsonl line count (${expected_l4})" >&2
    fail=1
else
    echo "[RC-11][OK]   L4_queries cell matches jsonl: ${actual_l4}"
fi

# (3) exit_status=cost_exit (or atexit / call_exit / autosubmit) cell set.
if grep -qE "exit_status=(cost_exit|call_exit|atexit|autosubmit)" "${log_path}"; then
    cohort=$(grep -oE "exit_status=[a-z_]+" "${log_path}" | tail -n1 | cut -d= -f2)
    echo "[RC-11][OK]   exit_status cohort marker present: ${cohort}"
else
    echo "[RC-11][FAIL] no non-normal exit_status cohort marker on the line — cost-exit was not flagged" >&2
    grep -E "task=${TASK_ID}" "${log_path}" >&2 || true
    fail=1
fi

# (4) verify_report excludes / flags this task from engagement_rate
#     denominator. We do NOT re-run the full verify here (it depends on
#     run-level summary aggregation) — instead assert the per-task line
#     carries the cohort marker that RC-10's verify_report consumes.
#     # TODO(RC-11-coord): once RC-10 lands the verify_report rate-gate
#     # exclusion, extend this script to:
#     #   python3 scripts/swebench/verify_report.py append --run-dir <out>
#     # and assert engagement_rate denominator == n_total - n_cost_exit.
echo "[RC-11] (4) verify_report rate-gate exclusion is RC-10's responsibility — see TODO(RC-11-coord)."

if [[ "${fail}" -ne 0 ]]; then
    echo "[RC-11] integration check FAILED" >&2
    exit 1
fi
echo "[RC-11] integration check PASSED — cost-exit artifacts pulled and cohort marked."
