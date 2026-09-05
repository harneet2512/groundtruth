#!/bin/sh
set -eu

output=$(cd gt-index && go test -tags sqlite_fts5 ./cmd/gt-index \
  -run TestCandidateContextListsEncodeNilAsEmptyArray -count=1 2>&1) && {
  printf '%s\n' 'candidate context encoding RED unexpectedly passed'
  exit 0
}

printf '%s\n' "$output" | grep -F 'undefined: marshalResolutionStringList' >/dev/null || {
  printf '%s\n' 'candidate context encoding RED failed for an unexpected reason'
  exit 2
}
printf '%s\n' 'RED: candidate context has no canonical empty-list encoder'
exit 1
