#!/usr/bin/env bash
# Decision-table test for the substrate_proof.sh proof-failure gate (A2).
# Mirrors the 3-way branch at scripts/ci/substrate_proof.sh:677-703 (OOM / _LEGIT abort /
# require-flag fail-closed / best-effort else). Asserts the exit behavior so the table can't
# silently regress to the old "launder ANY rc!=0 to ok" bug.
set -u
_FAIL=0

# Replica of the real decision block. Keep in lockstep with substrate_proof.sh.
decide() {
  local PROOF_RC="$1" fail_json="$2"
  local GT_REQUIRE_FULL_STACK="${GT_REQUIRE_FULL_STACK:-0}"
  local GT_REQUIRE_GRAPH_VALID="${GT_REQUIRE_GRAPH_VALID:-0}"
  local GT_REQUIRE_LSP="${GT_REQUIRE_LSP:-0}"
  local GT_REQUIRE_EMBEDDER="${GT_REQUIRE_EMBEDDER:-0}"
  local _LEGIT="COMMIT_PARITY|EVAL_LEAKAGE_FORBIDDEN|SUBSTRATE_NOT_PORTABLE|FINAL_PIPELINE_HOST_SPLIT_FAIL|GT_DEAD_SURFACE_LOADED"
  if [ "$PROOF_RC" -eq 137 ]; then echo "ABORT_OOM"; return 1; fi
  if echo "$fail_json" | grep -qE "$_LEGIT"; then echo "ABORT_LEGIT"; return 1; fi
  if [ "$GT_REQUIRE_FULL_STACK" = "1" ] || [ "$GT_REQUIRE_GRAPH_VALID" = "1" ] \
     || [ "$GT_REQUIRE_LSP" = "1" ] || [ "$GT_REQUIRE_EMBEDDER" = "1" ]; then
    echo "FAIL_CLOSED"; return 1
  fi
  echo "BEST_EFFORT_OK"; return 0
}

expect() { # desc  expected_label  expected_rc  actual_label  actual_rc
  if [ "$2" = "$4" ] && [ "$3" = "$5" ]; then
    echo "ok   - $1"
  else
    echo "FAIL - $1: expected ${2}/rc${3}, got ${4}/rc${5}"; _FAIL=1
  fi
}

# 1. Required-stack run + quality proof failure -> FAIL-CLOSED (the A2 fix; was BEST_EFFORT_OK).
out=$(GT_REQUIRE_LSP=1 decide 1 "EMBEDDER_USAGE_FAIL"); rc=$?
expect "GT_REQUIRE_LSP=1 + rc!=0 fails closed" FAIL_CLOSED 1 "$out" "$rc"
out=$(GT_REQUIRE_FULL_STACK=1 decide 1 "GT_INDEX_FAIL"); rc=$?
expect "GT_REQUIRE_FULL_STACK=1 + rc!=0 fails closed" FAIL_CLOSED 1 "$out" "$rc"
# 2. Unflagged iteration run + quality failure -> best-effort ok (PRODUCT RULE preserved).
out=$(decide 1 "GRAPH_CERT_INVALID"); rc=$?
expect "unflagged + rc!=0 proceeds best-effort" BEST_EFFORT_OK 0 "$out" "$rc"
# 3. Legitimacy breach aborts regardless of flags (must precede the require-flag check).
out=$(GT_REQUIRE_LSP=1 decide 1 "EVAL_LEAKAGE_FORBIDDEN"); rc=$?
expect "legitimacy breach aborts before flag check" ABORT_LEGIT 1 "$out" "$rc"
# 4. OOM always aborts.
out=$(decide 137 ""); rc=$?
expect "OOM (rc=137) aborts" ABORT_OOM 1 "$out" "$rc"

[ "$_FAIL" -eq 0 ] && { echo "ALL PASS"; exit 0; } || { echo "SOME FAILED"; exit 1; }
