#!/usr/bin/env bash
# Pre-smoke gate — must pass before any 10-task smoke is launched.
#
# These are BEHAVIOR tests, not implementation mirrors. Each one targets a
# real bug that previously shipped undetected and cost us a smoke run:
#
#   test_reporter_emits_hybrid_readiness    — caught the dormant gate
#   test_driver_propagates_lsp_env          — caught GT_LSP_ENABLED not
#                                              reaching the container
#   test_scraper_accepts_lsp_hybrid_label   — caught the arm-filter
#                                              rejecting gt-lsp-hybrid
#   test_gt_finalization_hybrid             — the readiness gate itself
#
# Usage (from repo root):
#   bash scripts/swebench/pre_smoke_gate.sh
#
# Exit code 0 means smoke is safe to launch. Non-zero means do NOT launch.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
cd "$REPO"

TESTS=(
  tests/unit/test_reporter_emits_hybrid_readiness.py
  tests/unit/test_reporter_rollups_basic_chain.py
  tests/unit/test_driver_propagates_lsp_env.py
  tests/unit/test_scraper_accepts_lsp_hybrid_label.py
  tests/unit/test_gt_finalization_hybrid.py
  tests/unit/test_verify_report_rate_contract.py
)

echo "=== pre_smoke_gate: $(date +%H:%M:%S) ==="
echo "repo=$REPO"
python -m pytest -q "${TESTS[@]}"
rc=$?
if [ $rc -ne 0 ]; then
  echo "=== pre_smoke_gate: FAIL — do NOT launch smoke ==="
  exit $rc
fi
echo "=== pre_smoke_gate: PASS — smoke is safe to launch ==="
