#!/bin/sh
# RED: parsed symbols carry no content address. Nothing on a store.Node names
# the bytes a symbol was extracted from, so a delivered snippet cannot be
# checked against the file it claims to come from -- the graph goes stale
# silently instead of reporting a mismatch.
#
# Go prints elapsed times, so they are normalised: this output is re-executed
# and compared byte-for-byte by .githooks/pre-push.
set -u
cd gt-index
tmp=$(mktemp)
go test -tags sqlite_fts5 ./internal/parser -count=1 \
  -run '^(TestEveryCodeSymbolCarriesAWholeAddress|TestAddressResolvesToTheExactDeclaration|TestAddressIsGrammarIndependent)$' \
  >"$tmp" 2>&1
rc=$?
sed -E "s/[0-9]+\.[0-9]+s/ELAPSED/g" "$tmp"
rm -f "$tmp"
exit $rc
