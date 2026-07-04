#!/usr/bin/env bash
# Test the merge-step completeness guard logic.
# Exercises three cases:
#   1. full300 + partial → failed_batches.json written, exit 1
#   2. smoke + partial → warning only, exit 0
#   3. any mode + zero → exit 1
#
# Usage: bash tests/workflows/test_merge_completeness.sh

set -euo pipefail

PASS=0
FAIL=0

run_case() {
  local desc="$1" mode="$2" expected="$3" total="$4" want_exit="$5"
  local tmpdir
  tmpdir=$(mktemp -d)
  trap "rm -rf $tmpdir" RETURN

  mkdir -p "$tmpdir/merged"

  # Create fake output.jsonl with $total lines
  for i in $(seq 1 "$total"); do
    echo '{"instance_id":"task-'$i'"}' >> "$tmpdir/merged/output.jsonl"
  done
  [ "$total" -eq 0 ] && : > "$tmpdir/merged/output.jsonl"

  # Create fake artifact dirs for received batches (batch 0..received-1)
  local tpj=5
  local n_expected=$(( (expected + tpj - 1) / tpj ))
  local n_received=$(( (total + tpj - 1) / tpj ))
  [ "$total" -eq 0 ] && n_received=0
  for i in $(seq 0 $(( n_received - 1 )) 2>/dev/null); do
    mkdir -p "$tmpdir/all_results/inference-test-batch-$i"
  done

  # Run the completeness check (extracted inline Python from the workflow)
  local got_exit=0
  python3 -c "
import json, os, glob, math, sys

expected = $expected
mode = '$mode'
tpj = $tpj
n_batches = math.ceil(expected / tpj) if expected > 0 else 0
expected_ids = set(range(n_batches))

received_dirs = glob.glob('$tmpdir/all_results/inference-*-batch-*')
received_ids = set()
for d in received_dirs:
    parts = d.rsplit('-batch-', 1)
    if len(parts) == 2 and parts[1].isdigit():
        received_ids.add(int(parts[1]))

missing = sorted(expected_ids - received_ids)
with open('$tmpdir/merged/failed_batches.json', 'w') as f:
    json.dump(missing, f)

total_lines = $total

if total_lines == 0:
    print('  exit 1: zero results')
    sys.exit(1)

if missing:
    print(f'  missing batches: {missing}')
    if mode == 'full300':
        print('  exit 1: full300 incomplete')
        sys.exit(1)
    else:
        print(f'  warning: partial {mode} run, continuing')
else:
    print(f'  all {n_batches} batches received')
" 2>&1 || got_exit=1

  # Verify failed_batches.json exists
  if [ ! -f "$tmpdir/merged/failed_batches.json" ]; then
    echo "  FAIL: failed_batches.json not written"
    FAIL=$((FAIL + 1))
    return
  fi

  if [ "$got_exit" -eq "$want_exit" ]; then
    echo "PASS: $desc (exit=$got_exit, want=$want_exit)"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $desc (exit=$got_exit, want=$want_exit)"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== Merge completeness guard tests ==="
echo

# Case 1: full300 + partial → hard fail
run_case "full300 partial (55/60 batches)" "full300" 300 275 1

# Case 2: smoke + partial → warn only
run_case "smoke partial (2/3 batches)" "smoke" 15 10 0

# Case 3a: smoke + zero → hard fail
run_case "smoke zero results" "smoke" 15 0 1

# Case 3b: full300 + zero → hard fail
run_case "full300 zero results" "full300" 300 0 1

# Case 4: full300 + complete → pass
run_case "full300 complete (all 60 batches)" "full300" 300 300 0

# Case 5: pilot20 + partial → warn only
run_case "pilot20 partial (3/4 batches)" "pilot20" 20 15 0

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
