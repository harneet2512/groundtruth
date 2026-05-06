#!/usr/bin/env bash
# RC-07 integration check — GT_INDEXES_ROOT propagates per-task to gt_track4_pre_run hook.
#
# WHAT IT PROVES
#   With the RC-07 fix in swe_agent_smoke_runner.py, a 3-task batch spanning
#   THREE different repos must produce three per-task briefs whose contents
#   reference symbols distinctive to each repo's own codebase. Before the fix,
#   every task's brief was generated against the FIRST task's graph.db, so a
#   Django task's brief was full of cfn-lint symbols (or vice versa).
#
#   Assertion contract:
#     for each task tid_i with repo_i:
#       brief_i must contain at least one symbol from REPO_DISTINCTIVE[repo_i]
#       brief_i must NOT contain a symbol from REPO_DISTINCTIVE[repo_j!=i]
#
# DO NOT RUN THIS SCRIPT ON A VM AS PART OF AUTOMATED CI.
#   It launches a real 3-task SWE-agent batch and costs LLM tokens. Treat as
#   a manual diagnostic invoked by the operator after a code change in the
#   smoke runner / pre-run hook env contract.
#
# USAGE
#   bash RC-07.sh            # uses defaults below; honors $GT_INDEXES_ROOT etc.
#   GT_INDEXES_ROOT=/data/eval_indexes bash RC-07.sh
#
# DEPENDENCIES
#   - python3 (for swe_agent_smoke_runner.py)
#   - jq (for parsing per-task gt_layers.log)
#   - per-instance graph.db's prebuilt under $GT_INDEXES_ROOT/<task_id>/graph.db
#     for each TASK_ID below
#
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
RUNNER="${REPO_ROOT}/scripts/swebench/swe_agent_smoke_runner.py"
CONFIG="${RC07_CONFIG:-${REPO_ROOT}/scripts/swebench/configs/track4_default.yaml}"
GT_INDEXES_ROOT="${GT_INDEXES_ROOT:-/home/ubuntu/eval_indexes}"
OUTPUT_DIR="${RC07_OUTPUT_DIR:-${REPO_ROOT}/.tmp_rc07_check_$(date +%s)}"

# Three tasks chosen from THREE distinct repos. Adjust if your prebuilt
# indexes use different ids; the goal is repo diversity, not specific tasks.
#   - Django:    a real django/django task id
#   - Flask:     a real pallets/flask task id
#   - cfn-lint:  a real aws-cloudformation/cfn-lint task id
TASK_IDS_DEFAULT="django__django-11099,pallets__flask-5014,aws-cloudformation__cfn-lint-2778"
TASK_IDS="${RC07_TASK_IDS:-${TASK_IDS_DEFAULT}}"

# Repo-distinctive symbols. Picked from each repo's public surface so a brief
# generated against the WRONG repo's graph.db cannot accidentally contain
# them. Keep one positive token and (implicitly) the other repos' tokens
# are the negative set.
declare -A POSITIVE
POSITIVE["django__django-11099"]="HttpResponse"            # django.http
POSITIVE["pallets__flask-5014"]="Flask"                    # flask.app
POSITIVE["aws-cloudformation__cfn-lint-2778"]="cfnlint"    # cfnlint package

# Build the cross-contamination negative set per task automatically.
declare -A NEGATIVE
NEGATIVE["django__django-11099"]="Flask cfnlint"
NEGATIVE["pallets__flask-5014"]="HttpResponse cfnlint"
NEGATIVE["aws-cloudformation__cfn-lint-2778"]="HttpResponse Flask"

mkdir -p "${OUTPUT_DIR}"

echo "[RC-07] runner:           ${RUNNER}"
echo "[RC-07] config:           ${CONFIG}"
echo "[RC-07] gt_indexes_root:  ${GT_INDEXES_ROOT}"
echo "[RC-07] task_ids:         ${TASK_IDS}"
echo "[RC-07] output_dir:       ${OUTPUT_DIR}"

# Sanity: each task must have a prebuilt graph.db. If any are missing, the
# fall-through path inside gt_track4_pre_run.py will mask the bug.
IFS=',' read -ra TIDS <<<"${TASK_IDS}"
for tid in "${TIDS[@]}"; do
    db="${GT_INDEXES_ROOT}/${tid}/graph.db"
    if [[ ! -f "${db}" ]]; then
        echo "[RC-07][FATAL] missing per-instance graph.db: ${db}" >&2
        echo "[RC-07] Build it with gt-index before running this check." >&2
        exit 2
    fi
done

# Launch the batch. We deliberately let the runner do the env propagation —
# that is what we are testing.
python3 "${RUNNER}" \
    --task-ids "${TASK_IDS}" \
    --config "${CONFIG}" \
    --output-dir "${OUTPUT_DIR}" \
    --gt-indexes-root "${GT_INDEXES_ROOT}" \
    --workers 1 \
    --skip-preflight

# After the run, locate per-task gt_layers.log files. SWE-agent emits one
# per instance under the trajectory directory; we look for any file matching
# gt_layers.log under the run output and key it by task id.
fail=0
for tid in "${TIDS[@]}"; do
    log_path=$(find "${OUTPUT_DIR}" -type f -name "gt_layers.log" -path "*${tid}*" | head -n1 || true)
    if [[ -z "${log_path}" ]]; then
        echo "[RC-07][FAIL] no gt_layers.log found for ${tid} under ${OUTPUT_DIR}" >&2
        fail=1
        continue
    fi
    brief_preview=$(jq -rs 'map(select(.event=="brief_preview" or .brief_preview != null) | .brief_preview // .event_data.brief_preview // "") | join("\n")' "${log_path}" 2>/dev/null || cat "${log_path}")
    pos="${POSITIVE[$tid]:-}"
    neg="${NEGATIVE[$tid]:-}"

    if [[ -n "${pos}" ]] && ! grep -q "${pos}" <<<"${brief_preview}"; then
        echo "[RC-07][FAIL] ${tid}: brief is missing positive symbol '${pos}'." >&2
        fail=1
    else
        echo "[RC-07][OK]   ${tid}: brief contains '${pos}'."
    fi
    for n in ${neg}; do
        if grep -q "${n}" <<<"${brief_preview}"; then
            echo "[RC-07][FAIL] ${tid}: brief contaminated with cross-repo symbol '${n}'." >&2
            fail=1
        fi
    done
done

if [[ "${fail}" -ne 0 ]]; then
    echo "[RC-07] integration check FAILED" >&2
    exit 1
fi
echo "[RC-07] integration check PASSED — per-task briefs are repo-distinctive."
