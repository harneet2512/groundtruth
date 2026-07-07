#!/usr/bin/env bash
# ROW-27 DELIVERY GATE (host-side, post-trial). EXTRACTED from swebench_live_lite_full.yml
# to stay under GHA's ~21k run-block expression limit. Reads the trial log and attests the
# GT delivery markers the container-side pre-agent emits, so a GT-off trajectory can NEVER
# be mis-read as green (the silent false-green from run 28844447328). Baseline arm exempt.
#
# Arg $1 = path to the trial log (default trial_output.log). Never fails the step (exit 0);
# it writes GT_DELIVERY_GATE=0 + GT_DELIVERY_FAIL lines the audit reads.
set -u
LOG="${1:-trial_output.log}"

# Baseline arm: GT is meant OFF — nothing to attest.
[ "${GT_BASELINE:-0}" = "1" ] && exit 0

GATE_OK=1
if grep -q "hook_attached: \[.*LocalEnvironment" "$LOG" 2>/dev/null; then
  echo "[GT] delivery-gate: agent hook ATTACHED (.pth active)" | tee -a "$LOG"
else
  echo "GT_DELIVERY_FAIL: agent hook did NOT attach (.pth inactive) -- per-turn GT delivered nothing" | tee -a "$LOG"
  GATE_OK=0
fi
if grep -q "mini_preagent ok" "$LOG" 2>/dev/null; then
  echo "[GT] delivery-gate: STEP-0 brief prepended (mini_preagent)" | tee -a "$LOG"
else
  echo "GT_DELIVERY_FAIL: STEP-0 pre-agent did NOT run -- RECEIVED@0 will be NO" | tee -a "$LOG"
  GATE_OK=0
fi
if grep -q "hook_graph_hash_matches_post_lsp=True" "$LOG" 2>/dev/null; then
  echo "[GT] delivery-gate: consumption WITNESS ok (hook == post-LSP graph, sec 14.4)" | tee -a "$LOG"
else
  echo "GT_DELIVERY_FAIL: consumption witness missing/mismatch (sec 14.4)" | tee -a "$LOG"
  GATE_OK=0
fi
echo "GT_DELIVERY_GATE=${GATE_OK}" | tee -a "$LOG"
exit 0
