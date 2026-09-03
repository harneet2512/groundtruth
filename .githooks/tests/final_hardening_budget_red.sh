#!/bin/sh
# RED: publication is unbudgeted, so a repository the analysis cannot finish is
# published as nothing at all rather than as a bounded graph that says where it
# stopped. Two halves are missing:
#   internal/store  -- a callsite cannot carry a budget abstention, so a stopped
#                      analysis is indistinguishable from a complete one.
#   cmd/gt-index    -- there is no budget knob, no typed budgeted dispatch, and
#                      the VTA iteration budget that already exists is never set.
#
# Go prints elapsed times, so they are normalised: this output is re-executed
# and compared byte-for-byte by .githooks/pre-push. -p 1 keeps the two package
# reports in command-line order.
set -u
cd gt-index
tmp=$(mktemp)
go test -tags sqlite_fts5 -p 1 -count=1 ./internal/store ./cmd/gt-index \
  -run '^(TestBudgetAbstentionIsPersistedOnCallsiteEdgeAndNode|TestUnknownCallsiteAbstentionReasonIsRejectedByName|TestHoistedVTAFactCacheStillRejectsConflictingRebinding|TestIndexBudgetsResolveFromEnvironment|TestBudgetedCallsiteAbstainsWithTypedReasonAndKeepsCandidates|TestFlowFactBudgetIsDisabledAtZeroAndInclusiveAboveIt|TestCandidateEdgesDoNotDuplicateCallsiteCoverage)$' \
  >"$tmp" 2>&1
rc=$?
sed -E 's/[0-9]+\.[0-9]+s/ELAPSED/g' "$tmp"
rm -f "$tmp"
exit $rc
