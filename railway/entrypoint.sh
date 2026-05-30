#!/usr/bin/env bash
# Railway one-shot entrypoint. GUARDED.
#
#   RUN_SMOKE != 1  -> env self-check only, EXIT 0, near-zero cost (no LLM).
#   RUN_SMOKE == 1  -> run the OH+GT agent loop on beetbox__beets-5495,
#                      tee logs to stdout, copy output.jsonl + gt_interactions
#                      to /workspace/results, EXIT (one-shot; Railway billing stops).
#
# Everything streams to stdout so `railway logs -f` shows GT inject + agent act live.

set -uo pipefail

INSTANCE_ID="${GT_INSTANCE_ID:-beetbox__beets-5495}"
WORKSPACE_DIR="${GT_WORKSPACE_ROOT:-/workspace}/${INSTANCE_ID}"
GRAPH_DB="${WORKSPACE_DIR}/graph.db"
OUT_DIR="${GT_OUT_DIR:-/workspace/results}"
DRIVER="/app/railway/run_one_task.py"

echo "=================================================================="
echo " GT+OH Railway one-shot — instance=${INSTANCE_ID} runtime=local"
echo " RUN_SMOKE=${RUN_SMOKE:-0}"
echo "=================================================================="

# ---------------------------------------------------------------------------
# Defensive: if gt-index is present but graph.db was not baked, build it now.
# (Image build may have skipped it; rebuild at start so the GT graph layer has data.)
# ---------------------------------------------------------------------------
if [ ! -f "${GRAPH_DB}" ] && command -v gt-index >/dev/null 2>&1; then
    echo "[entrypoint] graph.db missing — building with gt-index at start..."
    gt-index -root "${WORKSPACE_DIR}" -output "${GRAPH_DB}" \
        && echo "[entrypoint] graph.db built: ${GRAPH_DB}" \
        || echo "[entrypoint] WARN: gt-index run failed; GT graph layers degrade to fallback"
fi

# ===========================================================================
# ENV SELF-CHECK (always runs). Proves the image is sound BEFORE any paid run.
# ===========================================================================
echo "--- ENV SELF-CHECK ---"
RC=0

python - <<'PY' || RC=1
import sys
try:
    import openhands
    print(f"[check] openhands import OK -> {openhands.__file__}")
except Exception as e:
    print(f"[check] FAIL openhands import: {e}"); sys.exit(1)
try:
    import groundtruth
    print(f"[check] groundtruth import OK -> {groundtruth.__file__}")
except Exception as e:
    print(f"[check] FAIL groundtruth import: {e}"); sys.exit(1)
# The local-runtime + eval namespace the driver depends on.
try:
    from openhands.runtime import get_runtime_cls
    cls = get_runtime_cls("local")
    print(f"[check] runtime 'local' -> {cls.__module__}.{cls.__name__}")
except Exception as e:
    print(f"[check] FAIL get_runtime_cls('local'): {e}"); sys.exit(1)
try:
    import oh_gt_full_wrapper  # PYTHONPATH includes /app/scripts/swebench
    print("[check] oh_gt_full_wrapper import OK (GT hook layer reachable)")
except Exception as e:
    print(f"[check] FAIL oh_gt_full_wrapper import: {e}"); sys.exit(1)
PY

echo "[check] workspace dir:"
if [ -d "${WORKSPACE_DIR}/.git" ]; then
    echo "[check] ${WORKSPACE_DIR} OK ($(git -C "${WORKSPACE_DIR}" rev-parse --short HEAD 2>/dev/null))"
else
    echo "[check] FAIL: ${WORKSPACE_DIR} missing or no .git"; RC=1
fi

if [ -f "${GRAPH_DB}" ]; then
    echo "[check] graph.db present: ${GRAPH_DB} ($(stat -c%s "${GRAPH_DB}" 2>/dev/null || echo '?') bytes)"
else
    echo "[check] WARN: graph.db absent (GT graph layers will be limited)"
fi

# DeepSeek auth probe (cheap: max_tokens=5). Confirms the key Railway injected works.
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
    echo "[check] DeepSeek auth probe..."
    HTTP=$(curl -s -o /tmp/ds_probe.json -w '%{http_code}' \
        -X POST "https://api.deepseek.com/chat/completions" \
        -H "Authorization: Bearer ${DEEPSEEK_API_KEY}" \
        -H "Content-Type: application/json" \
        -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"ok"}],"max_tokens":5}')
    if [ "${HTTP}" = "200" ]; then
        echo "[check] DeepSeek auth OK (HTTP 200)"
    else
        echo "[check] FAIL: DeepSeek auth HTTP ${HTTP}"; cat /tmp/ds_probe.json; RC=1
    fi
else
    echo "[check] FAIL: DEEPSEEK_API_KEY not set"; RC=1
fi

echo "--- SELF-CHECK RC=${RC} ---"

# ===========================================================================
# GUARD: only run the paid agent loop when RUN_SMOKE=1.
# ===========================================================================
if [ "${RUN_SMOKE:-0}" != "1" ]; then
    echo "[entrypoint] RUN_SMOKE != 1 — self-check only, no LLM task. Exiting ${RC}."
    exit "${RC}"
fi

if [ "${RC}" != "0" ]; then
    echo "[entrypoint] RUN_SMOKE=1 but self-check FAILED (RC=${RC}). Refusing to spend on a broken image."
    exit "${RC}"
fi

# ===========================================================================
# PAID RUN: OH+GT agent loop on the single task. Stream everything.
# ===========================================================================
echo "=================================================================="
echo " RUN_SMOKE=1 — launching OH+GT agent loop (LIVE). Stream below."
echo "=================================================================="
mkdir -p "${OUT_DIR}" /tmp/gt_debug
set -o pipefail
python "${DRIVER}" 2>&1 | tee /tmp/gt_debug/run_one_task.log
DRIVER_RC=${PIPESTATUS[0]}

echo "=================================================================="
echo " AGENT LOOP DONE (driver rc=${DRIVER_RC}). Artifacts:"
echo "=================================================================="
ls -la "${OUT_DIR}" 2>/dev/null || true
# Surface the patch size + interaction count for a quick eyeball.
if [ -f "${OUT_DIR}/output.jsonl" ]; then
    python - <<PY || true
import json
rec=json.loads(open("${OUT_DIR}/output.jsonl").read().splitlines()[0])
p=rec.get("git_patch","") or rec.get("test_result",{}).get("git_patch","")
print(f"[result] git_patch bytes={len(p)} history_events={len(rec.get('history',[]))}")
print("[result] patch preview:\n" + "\n".join(p.splitlines()[:40]))
PY
fi
echo "[entrypoint] one-shot complete. Exiting ${DRIVER_RC} (Railway billing stops)."
exit "${DRIVER_RC}"
