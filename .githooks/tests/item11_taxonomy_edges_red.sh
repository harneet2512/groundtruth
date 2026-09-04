#!/bin/sh
# RED: internal/taxonomy.DeriveEdges produces the five additive edge kinds of
# item 11 (delta row 9) and is unit-tested, but nothing calls it, so no row
# reaches the graph. These two tests drive the compiled binary over a real
# TypeScript repository: the first asserts the rows are IN THE DATABASE and
# that every persisted row carries a mechanism its kind grants and a tier that
# mechanism earns; the second asserts the pre-existing edge and label totals on
# the same fixture do not move.
#
# Go prints elapsed times, so they are normalised: this output is re-executed
# and compared byte-for-byte by .githooks/pre-push.
set -u
cd gt-index
tmp=$(mktemp)
go test -tags sqlite_fts5 ./cmd/gt-index -count=1 \
  -run '^(TestTaxonomyEdgesReachTheGraph|TestTaxonomyDoesNotMoveAPreExistingEdgeTotal)$' \
  >"$tmp" 2>&1
rc=$?
sed -E 's/[0-9]+\.[0-9]+s/ELAPSED/g' "$tmp"
rm -f "$tmp"
exit $rc
