#!/bin/sh
# RED: a partial-VTA callsite merges conservative hierarchy candidates into its
# published set, then claims the "vta" mechanism over all of them. Those merged
# candidates carry no flow proof, so the store rejects the callsite with
# "variable_type_flow requires typed source or propagation facts" -- and that
# rejection discards the ENTIRE atomic graph publication.
set -u
cd gt-index
go test -tags sqlite_fts5 ./cmd/gt-index -count=1 \
  -run '^TestVTACallsiteMechanismMatchesPublishedCandidateSet$' 2>&1
