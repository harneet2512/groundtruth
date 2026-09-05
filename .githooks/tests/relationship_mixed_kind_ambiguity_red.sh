#!/bin/sh
set -eu

output=$(cd gt-index && go test -tags sqlite_fts5 ./internal/resolver \
  -run TestResolveClassOrFuncNodeAbstainsAcrossDeclarationKinds -count=1 2>&1) && {
  printf '%s\n' 'mixed-kind relationship ambiguity RED unexpectedly passed'
  exit 0
}

printf '%s\n' "$output" | grep -F 'mixed ambiguous cross-file declarations resolved to' >/dev/null || {
  printf '%s\n' 'mixed-kind relationship ambiguity RED failed for an unexpected reason'
  exit 2
}
printf '%s\n' 'RED: JSX relationship resolution ignores ambiguity across declaration kinds'
exit 1
