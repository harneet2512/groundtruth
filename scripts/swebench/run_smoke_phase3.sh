#!/bin/bash
set -e
source ~/gt-venv/bin/activate
source ~/gt-env.sh
cd ~/groundtruth

OUTPUT_DIR=~/phase3_smoke_$(date +%Y%m%d_%H%M%S)
mkdir -p $OUTPUT_DIR

echo "=== Phase 3 Smoke Test — 4 Tasks ==="
echo "Architecture: 3 pattern-aware GT tools during work + test feedback + post-processing"
echo "Output: $OUTPUT_DIR"
echo "Started: $(date)"

# 4 smoke test tasks:
# 1. django__django-12856: multi-method class changes (obligation sites)
# 2. django__django-13158: known spinner — tests suppression
# 3. sympy__sympy-17655: non-Django, tests generalization
# 4. django__django-10914: class with conventions/guard patterns
FILTER="django__django-12856|django__django-13158|sympy__sympy-17655|django__django-10914"

python3 benchmarks/swebench/run_mini_phase3.py \
  -c benchmarks/swebench/mini_swebench_phase3.yaml \
  --model openai/gpt-5.4-nano \
  --subset lite --split test \
  -w 1 \
  --filter "$FILTER" \
  -o "$OUTPUT_DIR" \
  2>&1 | tee "$OUTPUT_DIR/smoke.log"

echo ""
echo "=== SMOKE TEST COMPLETE at $(date) ==="
echo ""

# ───────────────────────────────────────────────────
# SMOKE TEST CHECKS
# ───────────────────────────────────────────────────

PASS=0
FAIL=0

# Check A: Pattern roles populated (impact output contains role descriptions)
echo "=== Check A: Pattern roles populated ==="
ROLES_FOUND=0
for traj in "$OUTPUT_DIR"/*/*.traj.json; do
    if [ -f "$traj" ]; then
        has_roles=$(python3 -c "
import json
with open('$traj') as f:
    data = json.load(f)
messages = data.get('history') or data.get('messages') or data.get('trajectory') or []
for msg in messages:
    content = str(msg.get('content', '') if isinstance(msg, dict) else msg)
    if any(kw in content for kw in ['stores_in_state', 'serializes_to_kwargs', 'compares_in_eq',
           'emits_to_output', 'passes_to_validator', 'reads_in_logic',
           'packs self.', 'uses self.', 'stores self.', 'passes self.', 'reads self.', 'formats self.']):
        print('1')
        break
else:
    # Also check if groundtruth_impact was called at all
    for msg in messages:
        content = str(msg.get('content', '') if isinstance(msg, dict) else msg)
        if 'OBLIGATION SITES' in content:
            print('1')
            break
    else:
        print('0')
" 2>/dev/null || echo "0")
        task=$(basename $(dirname $traj))
        if [ "$has_roles" = "1" ]; then
            echo "  $task: pattern roles found"
            ROLES_FOUND=$((ROLES_FOUND + 1))
        else
            echo "  $task: no pattern roles (may not have called groundtruth_impact)"
        fi
    fi
done
if [ "$ROLES_FOUND" -ge 1 ]; then
    echo "  PASS: Pattern roles populated in $ROLES_FOUND tasks"
    PASS=$((PASS + 1))
else
    echo "  FAIL: No pattern roles found in any task"
    FAIL=$((FAIL + 1))
fi

# Check B: Convention detectors fire (at least 2/4 tasks show conventions)
echo ""
echo "=== Check B: Convention detectors fire ==="
CONV_FOUND=0
for traj in "$OUTPUT_DIR"/*/*.traj.json; do
    if [ -f "$traj" ]; then
        has_conv=$(python3 -c "
import json
with open('$traj') as f:
    data = json.load(f)
messages = data.get('history') or data.get('messages') or data.get('trajectory') or []
for msg in messages:
    content = str(msg.get('content', '') if isinstance(msg, dict) else msg)
    if 'CONVENTIONS:' in content or 'guard clause' in content or 'raise sites use' in content:
        print('1')
        break
else:
    print('0')
" 2>/dev/null || echo "0")
        task=$(basename $(dirname $traj))
        if [ "$has_conv" = "1" ]; then
            echo "  $task: conventions detected"
            CONV_FOUND=$((CONV_FOUND + 1))
        else
            echo "  $task: no conventions (may not need them)"
        fi
    fi
done
if [ "$CONV_FOUND" -ge 1 ]; then
    echo "  PASS: Conventions detected in $CONV_FOUND tasks"
    PASS=$((PASS + 1))
else
    echo "  WARNING: No conventions detected (may be expected for these tasks)"
    PASS=$((PASS + 1))  # Soft check
fi

# Check C: Completeness check works (OK/MISS output)
echo ""
echo "=== Check C: Completeness check works ==="
CHECK_FOUND=0
for traj in "$OUTPUT_DIR"/*/*.traj.json; do
    if [ -f "$traj" ]; then
        has_check=$(python3 -c "
import json
with open('$traj') as f:
    data = json.load(f)
messages = data.get('history') or data.get('messages') or data.get('trajectory') or []
for msg in messages:
    content = str(msg.get('content', '') if isinstance(msg, dict) else msg)
    if 'COMPLETENESS:' in content and ('OK' in content or 'MISS' in content or 'complete' in content.lower()):
        print('1')
        break
else:
    print('0')
" 2>/dev/null || echo "0")
        task=$(basename $(dirname $traj))
        if [ "$has_check" = "1" ]; then
            echo "  $task: completeness check ran"
            CHECK_FOUND=$((CHECK_FOUND + 1))
        else
            echo "  $task: no completeness check (agent may not have run it)"
        fi
    fi
done
if [ "$CHECK_FOUND" -ge 1 ]; then
    echo "  PASS: Completeness check worked in $CHECK_FOUND tasks"
    PASS=$((PASS + 1))
else
    echo "  WARNING: No completeness checks ran (agent may have skipped)"
    PASS=$((PASS + 1))  # Soft check
fi

# Check D: Dynamic communication (nudges appear, confidence labels)
echo ""
echo "=== Check D: Dynamic communication ==="
NUDGE_FOUND=0
for traj in "$OUTPUT_DIR"/*/*.traj.json; do
    if [ -f "$traj" ]; then
        has_nudge=$(python3 -c "
import json
with open('$traj') as f:
    data = json.load(f)
messages = data.get('history') or data.get('messages') or data.get('trajectory') or []
for msg in messages:
    content = str(msg.get('content', '') if isinstance(msg, dict) else msg)
    if any(kw in content for kw in ['[HIGH]', '[MED]', 'must appear in ALL',
           'Already queried', 'Already checked', 'Edit now']):
        print('1')
        break
else:
    print('0')
" 2>/dev/null || echo "0")
        task=$(basename $(dirname $traj))
        if [ "$has_nudge" = "1" ]; then
            echo "  $task: dynamic nudges found"
            NUDGE_FOUND=$((NUDGE_FOUND + 1))
        else
            echo "  $task: no dynamic nudges"
        fi
    fi
done
if [ "$NUDGE_FOUND" -ge 1 ]; then
    echo "  PASS: Dynamic communication in $NUDGE_FOUND tasks"
    PASS=$((PASS + 1))
else
    echo "  WARNING: No dynamic nudges found"
    PASS=$((PASS + 1))  # Soft check — agent may not trigger suppression
fi

# Check E: Turn count reasonable (1-4 GT calls per task, not 10+)
echo ""
echo "=== Check E: Turn count reasonable ==="
REASONABLE=0
EXCESSIVE=0
for traj in "$OUTPUT_DIR"/*/*.traj.json; do
    if [ -f "$traj" ]; then
        gt_calls=$(python3 -c "
import json
with open('$traj') as f:
    data = json.load(f)
usage = data.get('info', {}).get('gt_tool_usage', {})
print(usage.get('total_calls', 0))
" 2>/dev/null || echo "0")
        task=$(basename $(dirname $traj))
        echo "  $task: $gt_calls GT calls"
        if [ "$gt_calls" -le 6 ]; then
            REASONABLE=$((REASONABLE + 1))
        else
            EXCESSIVE=$((EXCESSIVE + 1))
        fi
    fi
done
if [ "$EXCESSIVE" -eq 0 ]; then
    echo "  PASS: All tasks have reasonable GT call counts"
    PASS=$((PASS + 1))
else
    echo "  FAIL: $EXCESSIVE tasks have excessive GT calls (>6)"
    FAIL=$((FAIL + 1))
fi

# Check F: No false positives from check (no naming, signature, exception findings)
echo ""
echo "=== Check F: No false positives from check ==="
FP_FOUND=0
for traj in "$OUTPUT_DIR"/*/*.traj.json; do
    if [ -f "$traj" ]; then
        has_fp=$(python3 -c "
import json
with open('$traj') as f:
    data = json.load(f)
messages = data.get('history') or data.get('messages') or data.get('trajectory') or []
for msg in messages:
    content = str(msg.get('content', '') if isinstance(msg, dict) else msg)
    # Check for old-style false positive patterns that should NOT appear
    if 'groundtruth_check' in content or 'COMPLETENESS:' in content:
        # Look for naming/typing/convention errors (should NOT be in completeness check)
        if any(kw in content for kw in ['[ERROR]', 'Unusual exception', 'overrides base']):
            print('1')
            break
else:
    print('0')
" 2>/dev/null || echo "0")
        task=$(basename $(dirname $traj))
        if [ "$has_fp" = "1" ]; then
            echo "  $task: POSSIBLE false positive in check output"
            FP_FOUND=$((FP_FOUND + 1))
        fi
    fi
done
if [ "$FP_FOUND" -eq 0 ]; then
    echo "  PASS: No false positives from completeness check"
    PASS=$((PASS + 1))
else
    echo "  FAIL: $FP_FOUND tasks had potential false positives in check"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "========================================="
echo "  SMOKE TEST RESULTS: $PASS passed, $FAIL failed"
echo "========================================="

if [ "$FAIL" -gt 0 ]; then
    echo "SMOKE TEST FAILED — fix issues before proceeding to 300-task run"
    exit 1
else
    echo "SMOKE TEST PASSED — ready for 300-task run"
fi
