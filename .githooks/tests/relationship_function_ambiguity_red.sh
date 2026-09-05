#!/bin/sh
set -eu

output=$(cd gt-index && go test -tags sqlite_fts5 ./internal/resolver \
  -run TestResolveClassOrFuncNodeAbstainsOnAmbiguousCrossFileFunction -count=1 2>&1) && {
  printf '%s\n' 'relationship ambiguity RED unexpectedly passed'
  exit 0
}

printf '%s\n' "$output" | grep -F 'ambiguous cross-file function resolved to' >/dev/null || {
  printf '%s\n' 'relationship ambiguity RED failed for an unexpected reason'
  exit 2
}
printf '%s\n' 'RED: JSX relationship resolution selects an arbitrary cross-file function'
exit 1
