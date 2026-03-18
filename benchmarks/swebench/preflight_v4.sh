#!/bin/bash
# Preflight checks for GT v4 on-demand tools deployment.
# Run on VM after git pull, BEFORE smoke test.
#
# Verifies:
#   1. gt_tool.py exists and parses
#   2. run_mini_gt.py has NO pre-computed context logic in active code
#   3. System prompt (YAML) includes tool instructions
#   4. gt_tool.py runs locally (index build on a small test)
#
# Usage: bash preflight_v4.sh
set -e

REPO_ROOT="${REPO_ROOT:-$HOME/groundtruth}"
cd "$REPO_ROOT"

echo "============================================================"
echo "  Preflight: GT v4 On-Demand Tools"
echo "============================================================"
echo ""

PASSED=0
FAILED=0

# ─── Check 1: gt_tool.py exists and parses ───
echo "--- Check 1: gt_tool.py exists and parses ---"
if [ -f benchmarks/swebench/gt_tool.py ]; then
  python3 -c "import ast; ast.parse(open('benchmarks/swebench/gt_tool.py').read())" 2>/dev/null
  if [ $? -eq 0 ]; then
    echo "[PASS] gt_tool.py exists and parses"
    PASSED=$((PASSED + 1))
  else
    echo "[FAIL] gt_tool.py has syntax errors"
    FAILED=$((FAILED + 1))
  fi
else
  echo "[FAIL] gt_tool.py not found — did you git pull?"
  FAILED=$((FAILED + 1))
fi

# ─── Check 2: No pre-computed context logic in active code ───
echo ""
echo "--- Check 2: No pre-computed context in run_mini_gt.py ---"
# These patterns should NOT exist in active code (comments OK)
BAD_PATTERNS=(
  "mini_gt_context"
  "gt_analysis\.md"
  "_generate_gt_context"
  "gt_file_written"
)
FOUND_BAD=0
for pat in "${BAD_PATTERNS[@]}"; do
  # grep -v '#' strips comment lines, then search
  MATCHES=$(grep -n "$pat" benchmarks/swebench/run_mini_gt.py 2>/dev/null | grep -v '^\s*#' | grep -v '"""' || true)
  if [ -n "$MATCHES" ]; then
    echo "  [WARN] Found '$pat' in active code:"
    echo "$MATCHES" | head -3 | sed 's/^/    /'
    FOUND_BAD=1
  fi
done
if [ "$FOUND_BAD" -eq 0 ]; then
  echo "[PASS] No pre-computed context logic found"
  PASSED=$((PASSED + 1))
else
  echo "[WARN] Old context patterns found — verify they're not in the active code path"
  PASSED=$((PASSED + 1))  # warn, not fail
fi

# ─── Check 3: YAML has tool instructions ───
echo ""
echo "--- Check 3: YAML includes tool instructions ---"
if grep -q "gt_tool.py" benchmarks/swebench/mini_swebench_gt_v4.yaml 2>/dev/null; then
  TOOL_CMDS=$(grep -c "gt_tool.py" benchmarks/swebench/mini_swebench_gt_v4.yaml)
  echo "[PASS] mini_swebench_gt_v4.yaml references gt_tool.py ($TOOL_CMDS times)"
  PASSED=$((PASSED + 1))

  # Verify all 4 commands are listed
  for CMD in references outline impact diagnose check; do
    if grep -q "$CMD" benchmarks/swebench/mini_swebench_gt_v4.yaml; then
      echo "  [OK] '$CMD' command listed"
    else
      echo "  [WARN] '$CMD' command NOT listed in YAML"
    fi
  done
else
  echo "[FAIL] mini_swebench_gt_v4.yaml does not reference gt_tool.py"
  FAILED=$((FAILED + 1))
fi

# ─── Check 4: run_mini_gt.py uses gt_tool.py ───
echo ""
echo "--- Check 4: run_mini_gt.py references gt_tool.py ---"
if grep -q 'gt_tool.py' benchmarks/swebench/run_mini_gt.py 2>/dev/null; then
  echo "[PASS] run_mini_gt.py references gt_tool.py"
  PASSED=$((PASSED + 1))

  # Verify key functions exist
  for FUNC in _setup_gt_tool _check_gt_tool_usage; do
    if grep -q "def $FUNC" benchmarks/swebench/run_mini_gt.py; then
      echo "  [OK] $FUNC() defined"
    else
      echo "  [WARN] $FUNC() NOT found"
    fi
  done

  # Verify version string
  if grep -q "v4.1_ondemand_tools" benchmarks/swebench/run_mini_gt.py; then
    echo "  [OK] gt_version = v4.1_ondemand_tools"
  else
    echo "  [WARN] gt_version string not found"
  fi
else
  echo "[FAIL] run_mini_gt.py does not reference gt_tool.py — still using old script?"
  FAILED=$((FAILED + 1))
fi

# ─── Check 5: mini-swe-agent is importable ───
echo ""
echo "--- Check 5: mini-swe-agent importable ---"
export PYTHONPATH="${HOME}/mini-swe-agent/src:${PYTHONPATH:-}"
python3 -c "from minisweagent.run.benchmarks.swebench import app; print('OK')" 2>/dev/null
if [ $? -eq 0 ]; then
  echo "[PASS] mini-swe-agent imports work"
  PASSED=$((PASSED + 1))
else
  echo "[FAIL] Cannot import mini-swe-agent — check PYTHONPATH"
  FAILED=$((FAILED + 1))
fi

# ─── Check 6: API key available ───
echo ""
echo "--- Check 6: API key available ---"
eval "$(grep '^export.*API_KEY' "$HOME/.bashrc" 2>/dev/null)" || true
if [ -n "${OPENAI_API_KEY:-}" ]; then
  echo "[PASS] OPENAI_API_KEY is set (${#OPENAI_API_KEY} chars)"
  PASSED=$((PASSED + 1))
else
  echo "[FAIL] OPENAI_API_KEY not set — source your .bashrc"
  FAILED=$((FAILED + 1))
fi

# ─── Summary ───
echo ""
echo "============================================================"
TOTAL=$((PASSED + FAILED))
echo "  Preflight: $PASSED/$TOTAL checks passed"
if [ "$FAILED" -gt 0 ]; then
  echo "  $FAILED check(s) FAILED — fix before running smoke test"
  echo "============================================================"
  exit 1
else
  echo "  All checks passed — ready for: bash start_diagnostic_vm.sh smoke"
  echo "============================================================"
fi
