#!/bin/sh
# RED: the store has nowhere to put a content address. `nodes` has no
# file_hash column, the node insert paths bind no address, and an unaddressed
# symbol is therefore indistinguishable from one addressed at byte 0.
#
# Go prints elapsed times, so they are normalised: this output is re-executed
# and compared byte-for-byte by .githooks/pre-push.
set -u
cd gt-index
tmp=$(mktemp)
go test -tags sqlite_fts5 ./internal/store -count=1 \
  -run '^(TestNodesCarryAFileHashColumn|TestBatchInsertNodesPersistsTheAddress|TestUnaddressedSymbolStoresNullNotZero|TestBatchInsertNodesTxPersistsTheAddress)$' \
  >"$tmp" 2>&1
rc=$?
sed -E "s/[0-9]+\.[0-9]+s/ELAPSED/g" "$tmp"
rm -f "$tmp"
exit $rc
