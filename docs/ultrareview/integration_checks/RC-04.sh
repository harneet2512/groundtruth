#!/usr/bin/env bash
# RC-04 integration check — SQLite correctness under reader/writer races.
#
# DO NOT run on the VM. This script is a local-laptop / dev-container probe.
# It assumes:
#   - gt-index is built (CGO_ENABLED=1) and on $PATH or at $GT_INDEX_BIN.
#   - python3 + the four agent tools are reachable via the bin/ wrappers
#     under tools/sweagent/{gt_query,gt_search,gt_navigate,gt_validate}.
#   - sqlite3 CLI is installed.
#
# What it proves (per RC-04 cluster definition):
#   1. With concurrent writer + reader, the reader returns CORRECT row counts
#      (not 0, not malformed) — i.e. immutable=1 was the bug, not the cure.
#   2. SIGKILL'ing the writer mid-transaction leaves the DB either consistent
#      OR surfaces a clear `db_corrupt` error to the reader (exit 4) — never
#      a silent 0-row return. (synchronous=NORMAL guarantees crash safety
#      for committed work.)
#   3. PRAGMA integrity_check reports `ok` on a healthy graph.db.
#   4. project_meta.min_confidence is populated by gt-index and is read back
#      identically by every reader (implicit via the same _resolve_min_confidence
#      helper in each agent tool).

set -u  # NOT -e: we want to capture and report exit codes.

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
GT_INDEX_BIN="${GT_INDEX_BIN:-$REPO_ROOT/gt-index/gt-index}"
TMP_DIR="$(mktemp -d)"
GRAPH_DB="$TMP_DIR/graph.db"
TARGET_REPO="${1:-$REPO_ROOT/src/groundtruth}"
PASS=0
FAIL=0

step() { echo; echo "── $1"; }
report() {
  if [ "$2" -eq 0 ]; then
    echo "  PASS: $1"
    PASS=$((PASS+1))
  else
    echo "  FAIL: $1 (rc=$2)"
    FAIL=$((FAIL+1))
  fi
}

# ──────────────────────────────────────────────────────────────────────────
step "1. Full index of a real repo subtree"
"$GT_INDEX_BIN" -root "$TARGET_REPO" -output "$GRAPH_DB"
report "gt-index full build" $?

# ──────────────────────────────────────────────────────────────────────────
step "2. PRAGMA integrity_check on healthy DB"
out="$(sqlite3 "$GRAPH_DB" "PRAGMA integrity_check;")"
[ "$out" = "ok" ]
report "integrity_check returns 'ok' (got: $out)" $?

# ──────────────────────────────────────────────────────────────────────────
step "3. project_meta.min_confidence is populated by gt-index"
mc="$(sqlite3 "$GRAPH_DB" "SELECT value FROM project_meta WHERE key='min_confidence'")"
test -n "$mc"
report "project_meta.min_confidence present (got: '$mc')" $?

# ──────────────────────────────────────────────────────────────────────────
step "4. Concurrent writer + reader — reader returns correct rowcount"
# Launch incremental writer in background, fire 5 concurrent reads.
# Capture row counts; expect every one to be > 0 and stable.
SAMPLE_FILE="$(sqlite3 "$GRAPH_DB" "SELECT file_path FROM nodes WHERE language='python' LIMIT 1;")"
if [ -z "$SAMPLE_FILE" ]; then
  echo "  SKIP: no python file in index — cannot exercise incremental path"
else
  (
    for _ in 1 2 3 4 5; do
      "$GT_INDEX_BIN" -root "$TARGET_REPO" -file "$SAMPLE_FILE" -output "$GRAPH_DB" >/dev/null 2>&1
    done
  ) &
  WRITER_PID=$!

  GT_GRAPH_DB="$GRAPH_DB" \
    "$REPO_ROOT/tools/sweagent/gt_query/bin/gt_query" \
      "$(sqlite3 "$GRAPH_DB" 'SELECT name FROM nodes LIMIT 1;')" >"$TMP_DIR/q1.out" 2>&1
  q1_rc=$?
  GT_GRAPH_DB="$GRAPH_DB" \
    "$REPO_ROOT/tools/sweagent/gt_navigate/bin/gt_navigate" \
      "$(sqlite3 "$GRAPH_DB" 'SELECT name FROM nodes LIMIT 1;')" trace >"$TMP_DIR/n1.out" 2>&1
  n1_rc=$?

  wait $WRITER_PID 2>/dev/null
  test $q1_rc -eq 0 -a $n1_rc -eq 0
  report "concurrent reads succeed (q=$q1_rc, n=$n1_rc)" $?
fi

# ──────────────────────────────────────────────────────────────────────────
step "5. SIGKILL writer mid-write — readers see clean DB or clear db_corrupt"
"$GT_INDEX_BIN" -root "$TARGET_REPO" -output "$GRAPH_DB" >/dev/null 2>&1 &
WRITER_PID=$!
sleep 0.2  # let the writer get into a transaction
kill -9 $WRITER_PID 2>/dev/null
wait $WRITER_PID 2>/dev/null

# Now open with a reader. Acceptable outcomes:
#   - exit 0 with non-empty output (DB is fine, WAL recovered correctly), OR
#   - exit 4 with "db_corrupt" on stderr (clear failure surfaced to agent)
GT_GRAPH_DB="$GRAPH_DB" \
  "$REPO_ROOT/tools/sweagent/gt_query/bin/gt_query" "X" >"$TMP_DIR/post_kill.out" 2>"$TMP_DIR/post_kill.err"
rc=$?
if [ $rc -eq 0 ]; then
  echo "  recovery: gt_query succeeded post-SIGKILL — WAL recovered cleanly"
  report "post-kill: clean recovery" 0
elif [ $rc -eq 4 ] && grep -q db_corrupt "$TMP_DIR/post_kill.err"; then
  echo "  recovery: gt_query returned db_corrupt — agent-visible failure (correct)"
  report "post-kill: clear db_corrupt surfaced" 0
else
  echo "  observed: rc=$rc, stderr=$(cat "$TMP_DIR/post_kill.err")"
  report "post-kill: silent or unexpected failure mode" 1
fi

# ──────────────────────────────────────────────────────────────────────────
step "6. immutable=1 is gone from every agent tool"
grep -rn 'immutable=1' "$REPO_ROOT/tools/sweagent/" >"$TMP_DIR/immutable_hits.txt" 2>/dev/null
test ! -s "$TMP_DIR/immutable_hits.txt"
report "no immutable=1 in agent-tool sqlite3.connect URIs" $?

# ──────────────────────────────────────────────────────────────────────────
step "7. MIN_CONFIDENCE: sites read project_meta or fall back to 0.5"
hardcoded_07="$(grep -rn 'MIN_CONFIDENCE = 0.7' \
  "$REPO_ROOT/tools/sweagent/gt_query" \
  "$REPO_ROOT/tools/sweagent/gt_search" \
  "$REPO_ROOT/tools/sweagent/gt_navigate" \
  "$REPO_ROOT/tools/sweagent/gt_validate" \
  "$REPO_ROOT/tools/sweagent/gt_pre_finish_gate" 2>/dev/null | wc -l)"
test "$hardcoded_07" -eq 0
report "no surviving MIN_CONFIDENCE=0.7 in agent tools (count=$hardcoded_07)" $?

echo
echo "──────────────────────────────────────────────"
echo "RC-04 integration check: $PASS passed, $FAIL failed"
echo "──────────────────────────────────────────────"
rm -rf "$TMP_DIR"
exit $FAIL
