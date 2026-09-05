#!/bin/sh
set -eu

output=$(cd gt-index && go test -tags sqlite_fts5 ./internal/store \
  -run TestAttachResolutionGraphPersistsTypedFactsWithoutCandidateConfidence \
  -count=1 2>&1) && {
  printf '%s\n' 'candidate provenance compaction RED unexpectedly passed'
  exit 0
}

printf '%s\n' "$output" | grep -F 'materialized_derivations=2' >/dev/null || {
  printf '%s\n' 'candidate provenance compaction RED failed for an unexpected reason'
  exit 2
}
printf '%s\n' 'RED: ordinary candidates still materialize one derivation node and fact edge each'
exit 1
