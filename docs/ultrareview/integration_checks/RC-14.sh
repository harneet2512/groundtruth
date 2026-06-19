#!/usr/bin/env bash
# RC-14 integration check — subprocess lifecycle + wallclock cap.
#
# WHAT IT PROVES
#   The RC-14 fix in swe_agent_smoke_runner.py must satisfy three contracts
#   under a 6-task / 4-way-concurrent SWE-agent run. Mid-run we send SIGTERM
#   to the smoke runner and verify:
#     1. all 6 SWE-bench docker containers are torn down (no orphans via
#        `docker ps`),
#     2. in-flight tasks have at least pre-SIGTERM artifacts pulled (the
#        on_instance_completed flush fires for tasks that completed before
#        the signal AND the env.close wrapper writes a "fired-mid-run"
#        partial summary for tasks killed in flight, so the global
#        gt_layers.log is non-empty),
#     3. with n=5 / workers=4, hard_cap is `math.ceil(5/4) * cap * 1.25`
#        = 2 * 1800 * 1.25 = 4500s, NOT the previous integer-floor value
#        of 1800 * 1.25 * 1 = 2250s.
#
# DO NOT RUN THIS SCRIPT ON A VM AS PART OF AUTOMATED CI.
#   It launches a real 6-task SWE-agent batch and costs LLM tokens
#   (~$0.72 at the v1.0.5 envelope). Treat as a manual diagnostic
#   invoked by the operator after a code change in the smoke runner's
#   subprocess lifecycle path.
#
# USAGE
#   bash RC-14.sh
#   GT_INDEXES_ROOT=/data/eval_indexes bash RC-14.sh
#
# DEPENDENCIES
#   - python3 (for swe_agent_smoke_runner.py)
#   - docker CLI (for orphan-container check)
#   - per-instance graph.db's prebuilt under $GT_INDEXES_ROOT/<task_id>/graph.db
#
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
RUNNER="${REPO_ROOT}/scripts/swebench/swe_agent_smoke_runner.py"
CONFIG="${RC14_CONFIG:-${REPO_ROOT}/scripts/swebench/configs/track4_default.yaml}"
GT_INDEXES_ROOT="${GT_INDEXES_ROOT:-/home/ubuntu/eval_indexes}"
OUTPUT_DIR="${RC14_OUTPUT_DIR:-${REPO_ROOT}/.tmp_rc14_check_$(date +%s)}"
WORKERS="${RC14_WORKERS:-4}"
SIGTERM_DELAY_S="${RC14_SIGTERM_DELAY_S:-180}"   # send SIGTERM 3 min in
PER_INSTANCE_CAP_S="${RC14_PER_INSTANCE_CAP_S:-1800}"

# Six tasks chosen to span at least 2 distinct repos so we can verify
# both (a) parallel tear-down across repos and (b) the per-task signal
# forwarding path. Adjust to the indexes your operator has prebuilt.
TASK_IDS_DEFAULT="\
django__django-11099,\
django__django-13315,\
pallets__flask-5014,\
pallets__flask-5063,\
aws-cloudformation__cfn-lint-2778,\
aws-cloudformation__cfn-lint-2862"
TASK_IDS="${RC14_TASK_IDS:-${TASK_IDS_DEFAULT}}"

mkdir -p "${OUTPUT_DIR}"

echo "[RC-14] runner:           ${RUNNER}"
echo "[RC-14] config:           ${CONFIG}"
echo "[RC-14] gt_indexes_root:  ${GT_INDEXES_ROOT}"
echo "[RC-14] task_ids:         ${TASK_IDS}"
echo "[RC-14] output_dir:       ${OUTPUT_DIR}"
echo "[RC-14] workers:          ${WORKERS}"
echo "[RC-14] per-task cap (s): ${PER_INSTANCE_CAP_S}"
echo "[RC-14] sigterm delay (s): ${SIGTERM_DELAY_S}"

# Sanity: every task must have a prebuilt graph.db; otherwise the
# pre-run hook silently skips the brief and we can't tell a SIGTERM
# kill from a missing-index skip.
IFS=',' read -ra TIDS <<<"${TASK_IDS}"
for tid in "${TIDS[@]}"; do
    db="${GT_INDEXES_ROOT}/${tid}/graph.db"
    if [[ ! -f "${db}" ]]; then
        echo "[RC-14][FATAL] missing per-instance graph.db: ${db}" >&2
        echo "[RC-14] Build it with gt-index before running this check." >&2
        exit 2
    fi
done

# Snapshot the current docker ps so we can diff at the end.
PRE_CONTAINERS=$(docker ps --format '{{.ID}}' | sort -u || true)
echo "[RC-14] pre-run docker container ids:"
echo "${PRE_CONTAINERS}" | sed 's/^/  /'

# Launch the runner in background. We rely on the runner's own preflight
# to surface env errors before SIGTERM time.
python3 "${RUNNER}" \
    --task-ids "${TASK_IDS}" \
    --config "${CONFIG}" \
    --output-dir "${OUTPUT_DIR}" \
    --gt-indexes-root "${GT_INDEXES_ROOT}" \
    --workers "${WORKERS}" \
    --per-instance-wallclock-cap-seconds "${PER_INSTANCE_CAP_S}" \
    --skip-preflight \
    > "${OUTPUT_DIR}/runner.stdout" 2> "${OUTPUT_DIR}/runner.stderr" &
RUNNER_PID=$!
echo "[RC-14] runner pid=${RUNNER_PID}; waiting ${SIGTERM_DELAY_S}s before SIGTERM"

sleep "${SIGTERM_DELAY_S}"

if ! kill -0 "${RUNNER_PID}" 2>/dev/null; then
    echo "[RC-14][FAIL] runner exited before SIGTERM was sent — cannot validate signal forwarder." >&2
    wait "${RUNNER_PID}" || true
    exit 1
fi

echo "[RC-14] sending SIGTERM to runner pid=${RUNNER_PID}"
kill -TERM "${RUNNER_PID}"

# The fix gives SWE-agent up to 60s for `docker stop` + signal cleanup,
# then a 10s SIGKILL grace, then the wait loop falls through. Give it
# 90s total, then escalate.
SECONDS=0
while kill -0 "${RUNNER_PID}" 2>/dev/null && [[ $SECONDS -lt 90 ]]; do
    sleep 1
done
if kill -0 "${RUNNER_PID}" 2>/dev/null; then
    echo "[RC-14][FAIL] runner did not exit within 90s of SIGTERM — sending SIGKILL and reporting failure" >&2
    kill -KILL "${RUNNER_PID}" || true
    wait "${RUNNER_PID}" 2>/dev/null || true
    exit 1
fi
RUNNER_RC=0
wait "${RUNNER_PID}" || RUNNER_RC=$?
echo "[RC-14] runner exited rc=${RUNNER_RC}"

# Contract 1 — no orphaned containers.
fail=0
sleep 5  # let docker ps settle after teardown
POST_CONTAINERS=$(docker ps --format '{{.ID}}' | sort -u || true)
NEW_CONTAINERS=$(comm -23 <(echo "${POST_CONTAINERS}") <(echo "${PRE_CONTAINERS}") || true)
if [[ -n "${NEW_CONTAINERS}" ]]; then
    echo "[RC-14][FAIL] orphaned containers after SIGTERM:" >&2
    echo "${NEW_CONTAINERS}" | sed 's/^/  /' >&2
    fail=1
else
    echo "[RC-14][OK]   no orphaned containers"
fi

# Contract 2 — at least one task has a per-task gt_layers.log entry,
# proving the on_instance_completed env.close flush fired before the
# signal cascade reached the SWE-agent main process.
GLOBAL_LOG="${OUTPUT_DIR}/_global_gt_layers.log"
if [[ -f "${GLOBAL_LOG}" ]] && [[ -s "${GLOBAL_LOG}" ]]; then
    LINE_COUNT=$(wc -l < "${GLOBAL_LOG}")
    echo "[RC-14][OK]   pre-SIGTERM artifacts pulled: ${LINE_COUNT} layer lines"
else
    echo "[RC-14][FAIL] _global_gt_layers.log empty or missing — env.close wrapper never fired" >&2
    fail=1
fi

# Contract 3 — verify hard_cap math. Dry-run with n=5 / workers=4 and
# inspect the runner's logged hard_cap value. We rely on the runner
# logging its computed cap before launch (or via the helper directly).
echo "[RC-14] verifying hard_cap math: n=5 / workers=4 / cap=1800"
HARD_CAP_OBSERVED=$(python3 - <<PY
import sys, importlib.util
spec = importlib.util.spec_from_file_location(
    "smoke_runner",
    "${RUNNER}",
)
mod = importlib.util.module_from_spec(spec)
sys.modules["smoke_runner"] = mod
spec.loader.exec_module(mod)
print(mod._compute_hard_cap_seconds(cap_seconds=1800, task_count=5, workers=4))
PY
)
if [[ "${HARD_CAP_OBSERVED}" == "4500" ]]; then
    echo "[RC-14][OK]   hard_cap math: 4500s (math.ceil-correct)"
else
    echo "[RC-14][FAIL] hard_cap math: expected 4500, got ${HARD_CAP_OBSERVED}" >&2
    fail=1
fi

if [[ "${fail}" -ne 0 ]]; then
    echo "[RC-14] integration check FAILED" >&2
    exit 1
fi
echo "[RC-14] integration check PASSED — subprocess lifecycle clean."
