#!/usr/bin/env bash
# RC-02 — Cost discipline integration check.
#
# Do NOT run on the VM. This script documents the 1-task probe that
# proves the cost-discipline stack (preflight surface, total-cost-limit
# CLI flag, proxy callback log, post-run reconciliation, IAM-403 fail-fast).
#
# Prerequisite: a running LiteLLM proxy on http://localhost:4000 backed by
# the Vertex MaaS Qwen3 route (scripts/swebench/litellm_proxy_qwen.yaml).
#
# Expected dollar cost: ~$0.12 (1 task * v1.0.5 envelope).

set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
RUN_DIR="${REPO}/.tmp_rc02_check_$(date +%s)"
TASK_ID="${RC02_TASK_ID:-amoffat__sh-744}"

mkdir -p "${RUN_DIR}"

# ---- Step 1: launch with --total-cost-limit 0.50 (1-task cap) -------------
# The runner must:
#   * print 'EXPECTED_COST: 1 tasks * $0.1200 each = $0.1200 (cap $0.50)'
#     to stdout BEFORE Popen
#   * set --agent.model.total_cost_limit 0.50 in the SWE-agent argv
#   * curl http://localhost:4000/health and pass the preflight
#   * unset GT_LLM_API_KEY and ANTHROPIC_API_KEY from its env

python3 "${REPO}/scripts/swebench/swe_agent_smoke_runner.py" \
  --config "${REPO}/config/gt_track4.yaml" \
  --task-ids "${TASK_ID}" \
  --output-dir "${RUN_DIR}" \
  --workers 1 \
  --total-cost-limit 0.50 \
  --per-task-cost-estimate 0.12 \
  --api-base http://localhost:4000/v1 \
  --launcher sweagent \
  2>&1 | tee "${RUN_DIR}/runner.log"

# ---- Step 2: assert preflight + EXPECTED_COST surfaces in stdout ----------
grep -F "EXPECTED_COST: 1 tasks" "${RUN_DIR}/runner.log" \
  || { echo "FAIL: EXPECTED_COST line missing"; exit 1; }
grep -F "(cap \$0.50)" "${RUN_DIR}/runner.log" \
  || { echo "FAIL: cap not surfaced"; exit 1; }
grep -E "preflight.* unset_env:GT_LLM_API_KEY" "${RUN_DIR}/runner.log" \
  || { echo "FAIL: GT_LLM_API_KEY not unset"; exit 1; }

# ---- Step 3: assert litellm_calls.jsonl exists with cost rows --------------
test -s "${RUN_DIR}/litellm_calls.jsonl" \
  || { echo "FAIL: litellm_calls.jsonl missing or empty"; exit 1; }

# Each row should have at least one of (response_cost|cost|cost_usd) > 0
python3 - "${RUN_DIR}/litellm_calls.jsonl" <<'PY'
import json, sys
total = 0.0
rows = 0
with open(sys.argv[1]) as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        rows += 1
        for k in ("response_cost", "cost", "cost_usd", "total_cost"):
            if k in rec:
                total += float(rec[k] or 0.0)
                break
print(f"litellm_calls.jsonl: rows={rows} total=${total:.4f}")
assert rows > 0, "no rows"
assert total > 0, "total cost is zero — model-name mismatch?"
PY

# ---- Step 4: reconcile against output.jsonl --------------------------------
grep -E "reconcile rows=.*proxy=.*sweagent=.*delta=" "${RUN_DIR}/runner.log" \
  || { echo "FAIL: reconciliation line missing"; exit 1; }

# Within 5% — the runner emits a WARN line if not.
if grep -q "reconcile.*WARN" "${RUN_DIR}/runner.log"; then
  echo "WARN: proxy/agent cost divergence > 5%; investigate"
  exit 2
fi

# ---- Step 5 (manual): IAM-403 fail-fast ------------------------------------
# Manually revoke the proxy's Vertex IAM (e.g., remove roles/aiplatform.user
# from the SA bound to vertex_project on the proxy), then rerun this script.
# Expected: preflight fails before SWE-agent launches with
#   "[smoke_runner][preflight][FAIL] proxy_iam_denied:..."
# Re-grant after.

echo "RC-02 integration check: PASS"
