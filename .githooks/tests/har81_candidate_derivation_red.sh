#!/bin/sh
# RED: the store aborts the entire atomic graph publication on two resolution
# mechanisms it should accept -- an empty Mechanism (which resolutionDerivation
# already maps to global_name) and unique_method (absent from its vocabulary).
#
# Go prints elapsed times, so they are normalised: this output is re-executed
# and compared byte-for-byte by .githooks/pre-push.
set -u
cd gt-index
tmp=$(mktemp)
go test -tags sqlite_fts5 ./internal/store -count=1 \
  -run '^(TestEmptyMechanismIsAcceptedBecauseDerivationMapsIt|TestMissingTargetIdentitiesAreRejectedAndNamed|TestBothMissingIdentitiesAreReportedTogether|TestEveryCallsiteMechanismIsMapped|TestUniqueMethodIsNameDerivedNotTypeDerived)$' \
  >"$tmp" 2>&1
rc=$?
sed -E 's/[0-9]+\.[0-9]+s/ELAPSED/g' "$tmp"
rm -f "$tmp"
exit $rc
