#!/bin/sh
# RED: a callsite can carry several candidates whose targets share one stable
# id (aiomonitor resolves seven distinct nodes named "set" to one). Each is
# published as its own CANDIDATE_TARGET edge, whose stable id is derived from
# (callsite, target stable id) with no ordinal -- so they collide:
#
#   insert v2 candidate edge: UNIQUE constraint failed: edges.stable_id
#
# Publication is atomic, so that discards the entire graph. The duplicates also
# inflate candidate_count, which is what decides candidate_only vs ambiguous.
set -u
cd gt-index
go test -tags sqlite_fts5 ./cmd/gt-index -count=1 \
  -run '^(TestDuplicateStableIdentitiesCollapseToOneCandidate|TestSelectedNodeSurvivesAsItsIdentityRepresentative|TestSurvivingCandidatesKeepTheirOrder|TestUnknownNodesAreLeftForTheConservationCheck)$' 2>&1
