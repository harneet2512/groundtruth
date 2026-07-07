#!/usr/bin/env bash
# GT container-side pre-agent setup — runs INSIDE the task container, before the stock
# mini-swe-agent launches. EXTRACTED from swebench_live_lite_full.yml's docker `bash -c`
# to stay under GHA's ~21k run-block expression limit (same reason substrate_proof.sh was
# extracted). Because this is a standalone script (NOT inside the workflow's single-quoted
# docker argument) apostrophes/single-quotes are safe here.
#
# Two jobs:
#   1) install gt_mini_patch via a site-packages .pth into the AGENT interpreter, so the
#      per-turn Environment.execute() wrap loads in the process that actually runs the
#      agent (a bare `--install` only patches the ephemeral install process = the dead-
#      delivery bug from gt_math run 28844447328);
#   2) run gt_agent.mini_preagent — the no-pier replay of GTMiniSweAgent.run()'s pre-loop
#      (handoff-assert + consumption-witness + fail-closed brief consume + single
#      <gt-task-brief> prepend + persist .groundtruth/BRIEF.md), writing the assembled
#      turn-0 instruction to /gt_out/gt_task.txt.
#
# EXIT 44  => proof-mode fail-closed (missing brief / witness mismatch): the caller MUST
#             abort the paid run BEFORE the agent launch (no model spend on a witness-less
#             or brief-less trajectory). EXIT 0 otherwise (baseline / success / non-proof
#             degrade to issue-only).
set -u
OUT_TASK=/gt_out/gt_task.txt

# Resolve the interpreter that actually runs the agent (uv-tool python, etc.) from the
# mini-swe-agent launcher shebang; fall back to python3/python.
AGENT_BIN=$(command -v mini-swe-agent || true)
AGENT_PY=$(command -v python3 || command -v python)
if [ -n "$AGENT_BIN" ]; then
  SB=$(sed -n '1s/^#!//p' "$AGENT_BIN"); FIRST=${SB%% *}
  case "$FIRST" in
    *env) REST=${SB#* }; CAND=${REST%% *} ;;
    *)    CAND=$FIRST ;;
  esac
  if [ -x "$CAND" ]; then
    AGENT_PY=$CAND
  else
    RES=$(command -v "$CAND" 2>/dev/null || true); [ -n "$RES" ] && AGENT_PY=$RES
  fi
fi

# BASELINE (control) arm: NO GT at all — task is the raw issue (byte-identical control).
if [ "${GT_BASELINE:-0}" = "1" ]; then
  echo "[GT] BASELINE mode: gt_mini_patch NOT installed (pure mini-swe-agent control arm)"
  cat /gt_out/issue.txt > "$OUT_TASK" 2>/dev/null || echo "Fix the issue in the repository." > "$OUT_TASK"
  echo "[GT] baseline: task = issue only (byte-identical control)"
  exit 0
fi

# 1) .pth install into the AGENT interpreter's site-packages.
if [ -f /opt/gt/gt_mini_patch.py ]; then
  SITE_DIR=$("$AGENT_PY" -c "import sysconfig;print(sysconfig.get_paths().get('purelib') or '')" 2>/dev/null)
  if [ -n "$SITE_DIR" ] && [ -d "$SITE_DIR" ]; then
    printf '%s\n' "import sys; sys.path.insert(0, '/opt/gt'); import gt_mini_patch" > "$SITE_DIR/zzz_gt_mini_patch.pth"
    echo "[GT] gt_mini_patch .pth installed: $SITE_DIR (agent_py=$AGENT_PY)"
    "$AGENT_PY" -c "import sys;sys.path.insert(0,'/opt/gt');import gt_mini_patch as g;print('[GT] hook_attached:', getattr(g,'_PATCHED_CLASSES',[]) or 'NONE')" 2>&1 | tail -1 \
      || echo "[GT] WARN: gt_mini_patch import raised in agent_py"
  else
    echo "[GT] WARN: agent site-packages unresolved (agent_py=$AGENT_PY) -- DELIVERY NOT INSTALLED"
  fi
  # Legacy in-proc call kept for host-side setup + proof marker (harmless).
  python /opt/gt/gt_mini_patch.py --install >/dev/null 2>&1 || true
fi

# 2) host-side pre-agent (parity with GTMiniSweAgent.run() pre-loop): assemble gt_task.txt.
#    FAIL-CLOSED in proof mode — a nonzero mini_preagent exit means no brief / witness.
if PYTHONPATH=/opt/gt "$AGENT_PY" -c "import gt_agent; gt_agent.mini_preagent('/gt_out/issue.txt', '$OUT_TASK')"; then
  echo "[GT] mini_preagent ok: handoff+witness+brief prepended -> $OUT_TASK"
else
  echo "GT_PREAGENT_FAIL: host-side pre-agent (witness/brief) failed" | tee -a /gt_out/preagent_fail.txt
  if [ "${GT_PROOF_MODE:-1}" = "1" ]; then
    echo "GT_PROOF_ABORT: fail-closed on pre-agent failure"
    exit 44
  fi
  cat /gt_out/issue.txt > "$OUT_TASK" 2>/dev/null || echo "Fix the issue in the repository." > "$OUT_TASK"
fi
exit 0
