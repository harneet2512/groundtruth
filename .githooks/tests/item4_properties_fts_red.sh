#!/bin/sh
# RED: `properties` carries every behavioural fact the engine's property_rank
# ranks on -- guard clauses, return shapes, boundary conditions -- and it has
# no full-text index at all, so the only way to reach "validates empty input"
# is a LIKE scan over the whole table. nodes_fts already proves FTS5 is in the
# build; properties has nothing.
#
# Go prints elapsed times, so they are normalised: this output is re-executed
# and compared byte-for-byte by .githooks/pre-push.
set -u
cd gt-index
tmp=$(mktemp)
go test -tags sqlite_fts5 ./internal/store -count=1 \
  -run '^(TestPropertiesFTSTableIsCreatedWithTheSchema|TestPropertiesFTSIsExternalContentOverProperties|TestPropertiesFTSCoversEveryPropertyRow|TestPropertiesFTSMatchReachesTheOwningSymbol|TestPropertiesFTSFiltersByKindColumn|TestPropertiesFTSSurvivesTheIncrementalDeleteReinsert|TestPropertiesFTSGrowsByExactlyTheRowsWritten)$' \
  >"$tmp" 2>&1
rc=$?
sed -E 's/[0-9]+\.[0-9]+s/ELAPSED/g' "$tmp"
rm -f "$tmp"
exit $rc
