#!/usr/bin/env bash
# GT write->test->fix retry loop (spec §13.12) for the stock mini-swe-agent / Pro path.
# Ports GTMiniSweAgent._run_with_test_retry (gt_agent.py:1660-1707) to a shell loop,
# because the stock agent has NO re-enterable run(): each attempt shells a fresh
# `mini-swe-agent` process (edits persist in the repo working tree; gt_mini_patch's
# per-interpreter latches start fresh -> correct per-attempt oracle state).
#
# HARNESS mechanics: runs for BOTH arms (GT-on and GT_BASELINE) so a paired measurement
# compares the same harness. GT CONTENT is unchanged. LEAK-SAFE: the verifier runs the
# repository's OWN visible test suite (gt_agent._retry_test_command; NEVER the hidden
# /tests/test.patch = FAIL_TO_PASS), and the retry feedback is an arm-neutral
# <test-feedback> tag (not <gt-*>). GT_SELF_VERIFY_ATTEMPTS (or legacy
# GT_RETRY_ON_VERIFIER_FAIL) unset/0 -> exactly ONE launch, byte-identical to pre-retry.
#
# Args: $1=model  $2=task_file  $3=config  $4=out_trajectory
set -u
MODEL="${1:?model}"; TASK_FILE="${2:?task}"; CONFIG="${3:?config}"; OUT_TRAJ="${4:?out}"

# Resolve a python that can import gt_agent (PYTHONPATH=/opt/gt) = the agent interpreter.
AGENT_BIN=$(command -v mini-swe-agent || true)
AGENT_PY=$(command -v python3 || command -v python)
if [ -n "$AGENT_BIN" ]; then
  SB=$(sed -n '1s/^#!//p' "$AGENT_BIN"); FIRST=${SB%% *}
  case "$FIRST" in
    *env) REST=${SB#* }; CAND=${REST%% *} ;;
    *)    CAND=$FIRST ;;
  esac
  if [ -x "$CAND" ]; then AGENT_PY=$CAND; else RES=$(command -v "$CAND" 2>/dev/null || true); [ -n "$RES" ] && AGENT_PY=$RES; fi
fi
GTPY() { PYTHONPATH=/opt/gt "$AGENT_PY" "$@"; }

launch() {  # $1 = -o trajectory path
  yes "" 2>/dev/null | mini-swe-agent --model "$MODEL" --task "$(cat "$TASK_FILE")" \
    --config "$CONFIG" -o "$1" 2>&1 || true
}

# Read the retry count DIRECTLY from the container env (docker -e forwards it) -- NOT via a
# python capture. The .pth bootstrap prints [GT_META] *_import_fallback banners to STDOUT
# whenever the /opt/src payloads are absent (the containerized substrate), which contaminated
# the old $(python -c ...) capture -> parsed as non-numeric -> silently 0 (disabled). Bash-
# direct read mirrors gt_agent._retry_count precedence + _RETRY_MAX cap, contamination-proof.
RETRIES="${GT_SELF_VERIFY_ATTEMPTS:-}"
[ -z "$RETRIES" ] && RETRIES="${GT_RETRY_ON_VERIFIER_FAIL:-0}"
case "$RETRIES" in ''|*[!0-9]*) RETRIES=0 ;; esac
[ "$RETRIES" -gt 2 ] && RETRIES=2
echo "[GT_RETRY_DBG] GT_SELF_VERIFY_ATTEMPTS='${GT_SELF_VERIFY_ATTEMPTS:-<unset>}' -> RETRIES=$RETRIES"

# OFF (default): exactly one launch to the canonical -o path (byte-identical to pre-retry).
if [ "$RETRIES" -le 0 ]; then
  echo "[GT_RETRY] disabled (attempts=0) -- single launch"
  launch "$OUT_TRAJ"
  exit 0
fi

TOTAL=$((RETRIES + 1))
# Sentinel-extract so the .pth [GT_META] startup banners cannot contaminate the value.
TEST_CMD=$(GTPY -c "import gt_agent;print('__GTV__%s__GTV__'%gt_agent._retry_test_command()[0])" 2>/dev/null | sed -n 's/.*__GTV__\(.*\)__GTV__.*/\1/p' | tail -1)
TIMEOUT="${GT_RETRY_TEST_TIMEOUT_SEC:-600}"
case "$TIMEOUT" in ''|*[!0-9]*) TIMEOUT=600 ;; esac
cp "$TASK_FILE" /gt_out/gt_orig_task.txt 2>/dev/null || true
echo "[GT_RETRY] enabled: retries=${RETRIES} (total attempts=${TOTAL}) timeout=${TIMEOUT}s"

ATTEMPT=1
while :; do
  ATRAJ="/gt_out/mini-swe-agent.trajectory.attempt${ATTEMPT}.json"
  launch "$ATRAJ"
  cp "$ATRAJ" "$OUT_TRAJ" 2>/dev/null || true          # canonical = the latest attempt
  [ "$ATTEMPT" -ge "$TOTAL" ] && break
  if command -v timeout >/dev/null 2>&1; then
    timeout "$TIMEOUT" bash -c "$TEST_CMD" > /gt_out/gt_retry_test_out.txt 2>&1; TEST_RC=$?
  else
    bash -c "$TEST_CMD" > /gt_out/gt_retry_test_out.txt 2>&1; TEST_RC=$?
  fi
  STATUS=$(GTPY -c "import gt_agent,sys; print('__GTV__%s__GTV__'%gt_agent.mini_retry_step(int(sys.argv[1]),'/gt_out/gt_retry_test_out.txt',int(sys.argv[2]),'/gt_out/gt_orig_task.txt','/gt_out/gt_next_task.txt'))" "$TEST_RC" "$ATTEMPT" 2>/dev/null | sed -n 's/.*__GTV__\(.*\)__GTV__.*/\1/p' | tail -1)
  [ -z "$STATUS" ] && STATUS="unverifiable"
  echo "[GT_RETRY] attempt=${ATTEMPT}/${TOTAL} status=${STATUS} rc=${TEST_RC}"
  [ "$STATUS" != "fail" ] && break                      # pass -> done; unverifiable -> stop
  mv /gt_out/gt_next_task.txt "$TASK_FILE" 2>/dev/null || break
  ATTEMPT=$((ATTEMPT + 1))
done
exit 0
