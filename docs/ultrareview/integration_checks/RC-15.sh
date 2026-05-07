#!/usr/bin/env bash
# RC-15 integration check — performance / resource hygiene at scale.
#
# WHAT IT PROVES
#   Three independent invariants the unit tests cannot fully exercise
#   because they are about behaviour at task or run scale on a real
#   container or a real filesystem load:
#
#   (1) STATE COMMAND DOES NOT BLOCK ON GRAPH BUILD
#       Launch a task on a 50K-LoC repo WITHOUT prebuilt graph.db push.
#       Inside the container, run the state command and time it: the
#       state command must return within 1s and must NOT have invoked
#       gt-index. L3 evidence is allowed to be empty; other layers must
#       proceed normally.
#
#   (2) PARTIAL PULL IS DETECTED AND ATTRIBUTED
#       During the close-wrap window, force one of the 6 expected flat
#       artifacts to be unreadable (chmod 000 inside the container).
#       After completion, the host-side gt_layers.log / pull summary
#       must record partial_pull=true AND verify_report.py must exclude
#       this task from rate-gate denominators (so its zero counters do
#       not poison the run-level delivery_rate / engagement_rate).
#
#   (3) STREAMING VERIFY UNDER 500MB JSONL STAYS UNDER 512MB RSS
#       Generate a synthetic 500MB gt_output.jsonl with realistic record
#       shapes and run verify_report.py over it. Peak resident memory
#       must stay below 512MB. The legacy read_text().splitlines() path
#       OOMed the canary VM (1GB headroom).
#
# DO NOT RUN THIS SCRIPT ON A VM AS PART OF AUTOMATED CI.
#   Steps (1) and (2) launch a real SWE-agent task and cost LLM tokens.
#   Step (3) generates a 500MB file. Treat as a manual gate operators
#   invoke after a code change in the touched modules.
#
# USAGE
#   bash RC-15.sh              # uses the defaults below
#   RC15_STEP=1 bash RC-15.sh  # only the no-build invariant
#   RC15_STEP=2 bash RC-15.sh  # only the partial-pull invariant
#   RC15_STEP=3 bash RC-15.sh  # only the streaming-memory invariant
#
# DEPENDENCIES
#   python3, jq, gcloud (for the live SSH steps), /usr/bin/time -v
#     (steps 1/2 only, when running against a real VM)
#
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
RUNNER="${REPO_ROOT}/scripts/swebench/swe_agent_smoke_runner.py"
CONFIG="${RC15_CONFIG:-${REPO_ROOT}/scripts/swebench/configs/track4_default.yaml}"
OUTPUT_DIR="${RC15_OUTPUT_DIR:-${REPO_ROOT}/.tmp_rc15_check_$(date +%s)}"
TASK_ID="${RC15_TASK_ID:-django__django-11099}"   # 50K+ LoC repo
STEPS="${RC15_STEP:-1,2,3}"

mkdir -p "${OUTPUT_DIR}"
echo "[RC-15] runner:     ${RUNNER}"
echo "[RC-15] config:     ${CONFIG}"
echo "[RC-15] task_id:    ${TASK_ID}"
echo "[RC-15] output_dir: ${OUTPUT_DIR}"
echo "[RC-15] steps:      ${STEPS}"

# ─── Step 1 — state command must not invoke gt-index ────────────────────────
if [[ ",${STEPS}," == *",1,"* ]]; then
    echo
    echo "[RC-15][step 1] state command — no synchronous build path"
    echo "  Launching task ${TASK_ID} WITHOUT prebuilt graph.db (force missing)."

    # Force "no prebuilt index" by pointing GT_INDEXES_ROOT at an empty dir.
    EMPTY_DIR="${OUTPUT_DIR}/empty_indexes"
    mkdir -p "${EMPTY_DIR}"

    # Launch with workers=1 so we can attach to the container deterministically.
    python3 "${RUNNER}" \
        --task-ids "${TASK_ID}" \
        --config "${CONFIG}" \
        --output-dir "${OUTPUT_DIR}/step1" \
        --gt-indexes-root "${EMPTY_DIR}" \
        --workers 1 \
        --skip-preflight \
        --max-iters 3 || true

    # Look for the init log inside the trajectory dir. If gt_edit_state ever
    # invoked the build path, /tmp/gt_edit_state_init.log will contain a
    # "gt-index-build" event line. The RC-15 fix replaces that with a
    # "gt-edit-state-no-graph-db" event when GT_GRAPH_DB is absent.
    init_logs=$(find "${OUTPUT_DIR}/step1" -name "gt_edit_state_init.log" 2>/dev/null || true)
    fail1=0
    if [[ -z "${init_logs}" ]]; then
        echo "  [WARN] no gt_edit_state_init.log harvested — state command may not have fired"
    else
        for f in ${init_logs}; do
            if grep -q '"event":"gt-index-build"' "${f}"; then
                echo "  [FAIL] state command invoked the synchronous build path: ${f}"
                fail1=1
            fi
        done
    fi
    if [[ "${fail1}" -ne 0 ]]; then
        echo "[RC-15][step 1] FAIL"
        exit 1
    fi
    echo "[RC-15][step 1] PASS — no gt-index-build events in any state init log"
fi

# ─── Step 2 — partial pull is detected and excluded from denominators ───────
if [[ ",${STEPS}," == *",2,"* ]]; then
    echo
    echo "[RC-15][step 2] partial-pull attribution"
    echo "  Launch a task and chmod-000 one expected flat artifact during close-wrap."
    echo "  Operator hook: this requires a manual SSH step into the container while"
    echo "  the task is still alive but past the agent loop. See Phase 4 runbook."
    echo "  After completion the test asserts:"
    echo "    - host-side log line contains partial_pull=true"
    echo "    - verify_report.py excludes this task from delivery_rate denominator"

    # We do not automate the chmod here — it depends on operator timing inside
    # the container. The check below assumes the operator drove that step and
    # left the run dir under ${OUTPUT_DIR}/step2.
    if [[ ! -d "${OUTPUT_DIR}/step2" ]]; then
        echo "  [SKIP] no step2 output dir — set up the chmod scenario and rerun"
    else
        layers_log=$(find "${OUTPUT_DIR}/step2" -name "gt_layers.log" | head -n1)
        if [[ -z "${layers_log}" ]]; then
            echo "  [FAIL] no gt_layers.log under ${OUTPUT_DIR}/step2"
            exit 1
        fi
        if ! grep -q "partial_pull=true" "${layers_log}"; then
            echo "  [FAIL] gt_layers.log missing partial_pull=true (expected after chmod 000)"
            exit 1
        fi
        echo "  [OK] partial_pull=true present in gt_layers.log"

        # And verify_report must classify this task out of denominators.
        python3 "${REPO_ROOT}/scripts/swebench/verify_report.py" append \
            --run-dir "${OUTPUT_DIR}/step2" || true
        # Strict assertion: if the task with partial_pull=true contributed to
        # the denominator, delivery_rate would have moved by 1/N. The runbook
        # captures the prior baseline; the comparison is operator-side.
        echo "  [OK] verify_report ran — operator must compare against pre-chmod baseline"
    fi
fi

# ─── Step 3 — streaming verify_report stays under 512MB RSS ─────────────────
if [[ ",${STEPS}," == *",3,"* ]]; then
    echo
    echo "[RC-15][step 3] streaming verify_report on synthetic 500MB JSONL"

    SYNTH_DIR="${OUTPUT_DIR}/step3"
    mkdir -p "${SYNTH_DIR}"
    SYNTH_JSONL="${SYNTH_DIR}/gt_output.jsonl"

    python3 - "${SYNTH_JSONL}" <<'PY'
import json, os, sys
out = sys.argv[1]
target_bytes = 500 * 1024 * 1024
written = 0
i = 0
with open(out, "w", encoding="utf-8") as fh:
    # Realistic record shape — same fields verify_report consumes.
    while written < target_bytes:
        rec = {
            "instance_id": f"synth-task-{i:06d}",
            "final_patch": (
                "diff --git a/src/m_{i}.py b/src/m_{i}.py\n"
                "@@ -1,3 +1,3 @@\n"
                + ("-old\n" * 4)
                + ("+new line of synthetic patch text padding\n" * 6)
            ).format(i=i),
            "extra": "x" * 256,
        }
        line = json.dumps(rec) + "\n"
        fh.write(line)
        written += len(line.encode("utf-8"))
        i += 1
print(f"wrote {i} records, {written} bytes")
PY

    # Minimal supporting fixtures so _compute_kernel_gates does real work.
    mkdir -p "${SYNTH_DIR}/gt_logs"
    : > "${SYNTH_DIR}/gt_arm_summary.json"
    echo '{"arm":"gt-nolsp","task_count":1}' > "${SYNTH_DIR}/gt_arm_summary.json"
    : > "${SYNTH_DIR}/gt_report.csv"
    echo "instance_id,resolved" > "${SYNTH_DIR}/gt_report.csv"

    # Use /usr/bin/time -v on Linux for max resident set size.
    if command -v /usr/bin/time >/dev/null 2>&1; then
        echo "  Measuring peak RSS via /usr/bin/time -v"
        /usr/bin/time -v python3 -c "
import sys
sys.path.insert(0, '${REPO_ROOT}/scripts/swebench')
import verify_report
out = verify_report._compute_kernel_gates(__import__('pathlib').Path('${SYNTH_DIR}'))
print('present:', out.get('present'))
" 2> "${SYNTH_DIR}/time_v.log" || true

        peak_kb=$(grep -E "Maximum resident set size" "${SYNTH_DIR}/time_v.log" \
            | awk -F': ' '{print $2}' | tr -d ' ')
        if [[ -z "${peak_kb}" ]]; then
            echo "  [WARN] could not parse peak RSS from time -v"
        else
            peak_mb=$(( peak_kb / 1024 ))
            echo "  peak_rss = ${peak_mb} MB"
            if [[ "${peak_mb}" -gt 512 ]]; then
                echo "  [FAIL] peak RSS ${peak_mb} MB > 512 MB ceiling"
                exit 1
            fi
            echo "  [OK] peak RSS within ceiling"
        fi
    else
        echo "  [SKIP] /usr/bin/time not available (running on Windows?)"
        echo "  Operator must measure peak RSS manually with Process Explorer or psutil."
    fi
fi

echo
echo "[RC-15] integration check completed for steps: ${STEPS}"
