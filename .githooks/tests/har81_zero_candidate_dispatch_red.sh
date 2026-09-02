#!/bin/sh
set -eu
cd gt-index
go test -tags sqlite_fts5 ./cmd/gt-index \
  -run '^TestCandidateDispatchStatePreservesZeroCandidateAbstention$' -count=1
