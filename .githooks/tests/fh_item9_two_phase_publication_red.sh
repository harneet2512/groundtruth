#!/bin/sh
# RED: publication is one all-or-nothing transaction, so an analysis failure
# costs the core graph. Split it, and a graph can legitimately reach disk with
# the core layer committed and the resolution analysis rolled back -- which is
# the only way a repository whose analysis phase cannot finish ships anything
# at all. Every reader that verifies the completion receipt must then report a
# core-only graph by name, from an explicit analysis_state, instead of leaking
# the raw storage miss for a project_meta row that graph never wrote.
#
# Go prints elapsed times, so they are normalised: this output is re-executed
# and compared byte-for-byte by .githooks/pre-push.
set -u
cd gt-index
tmp=$(mktemp)
go test -tags sqlite_fts5 ./internal/store -count=1 \
  -run '^(TestCoreOnlyGraphIsReportedAsAnalysisAbsentNotAsAMissingRow|TestCoreOnlyGraphNamesAnalysisNotRunSeparatelyFromFailure|TestRejectedCandidateLeavesCoreGraphReadableAndNamed)$' \
  >"$tmp" 2>&1
rc=$?
sed -E 's/[0-9]+\.[0-9]+s/ELAPSED/g' "$tmp"
rm -f "$tmp"
exit $rc
