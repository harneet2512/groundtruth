#!/usr/bin/env bash
# B0 determinism gate — index a repo N times and assert the graph CONTENT is identical.
#
# Determinism means the graph the indexer produces is a pure function of the source, not of
# wall-clock time, goroutine scheduling, or Go map-iteration order. The three content sources
# fixed in B0: return_shape property order (parser.go), COMPOSES target selection
# (relationships.go), CONTAINS edge insert order (main.go) — each was a `range` over a Go map,
# which Go deliberately randomizes.
#
# We normalize away two NON-content storage artifacts before comparing:
#   * the file-hash timestamp        -> pinned via GT_INDEX_FIXED_TS (already wired, sqlite.go)
#   * SQLite page layout / freelist  -> VACUUM rewrites the file in canonical page order
# so a surviving hash difference is a REAL content nondeterminism (a determinism regression),
# not a cosmetic byte wobble.
#
# Usage: double_index_determinism.sh <gt-index-binary> <repo-root> [iterations]
set -u
BIN="${1:?usage: double_index_determinism.sh <binary> <repo> [iters]}"
REPO="${2:?repo root required}"
ITERS="${3:-4}"
export GT_INDEX_FIXED_TS="${GT_INDEX_FIXED_TS:-2026-01-01T00:00:00Z}"

PY="${PYTHON:-python}"
command -v "$PY" >/dev/null 2>&1 || PY=python3
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

declare -A hashes
for i in $(seq 1 "$ITERS"); do
  db="$tmp/idx_$i.db"
  "$BIN" -root "$REPO" -output "$db" >/dev/null 2>&1 || { echo "FAIL: index run $i errored"; exit 2; }
  "$PY" -c "import sqlite3,sys; sqlite3.connect(sys.argv[1]).execute('VACUUM')" "$db" \
    || { echo "FAIL: VACUUM run $i errored"; exit 2; }
  h="$($PY -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$db")"
  echo "run $i: ${h:0:24}"
  hashes["$h"]=1
done

n="${#hashes[@]}"
echo "----"
if [ "$n" -eq 1 ]; then
  echo "DETERMINISTIC: $ITERS indexings of $REPO produced byte-identical (VACUUM-normalized) graph.db"
  exit 0
else
  echo "NONDETERMINISTIC: $n distinct content hashes across $ITERS indexings — a determinism REGRESSION"
  exit 1
fi
