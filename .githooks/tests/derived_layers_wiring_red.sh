#!/bin/sh
# RED: the three landed analysis packages -- internal/cochange, internal/community
# and internal/process -- have no call site in the publication path, so the
# communities, community_members, processes and process_steps tables do not
# exist in a published graph and no derived_* outcome is recorded in
# project_meta. An operator cannot tell an analysis that found nothing from one
# that never ran.
#
# The timeout is raised because this fixture builds the indexer and indexes a
# repository with it, and a cold build cache in a fresh checkout costs minutes
# before the first assertion runs.
#
# Go prints elapsed times, so they are normalised: this output is re-executed
# and compared byte-for-byte by .githooks/pre-push.
set -u
cd gt-index
tmp=$(mktemp)
go test -tags sqlite_fts5 ./cmd/gt-index -count=1 -timeout 30m \
  -run '^TestDerivedLayersArePublishedByTheRealCLI$' \
  >"$tmp" 2>&1
rc=$?
sed -E 's/[0-9]+\.[0-9]+s/ELAPSED/g' "$tmp"
rm -f "$tmp"
exit $rc
