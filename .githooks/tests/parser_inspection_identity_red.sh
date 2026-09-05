#!/bin/sh
set -eu

output=$(cd gt-index && go test -tags sqlite_fts5 ./cmd/gt-index \
  -run 'TestInspectionRealBinaryIsBuildBoundByteStableAndExact|TestInspectionNegativeDispositionsAreExact' \
  -count=1 2>&1) && {
  printf '%s\n' 'parser inspection identity RED unexpectedly passed'
  exit 0
}

printf '%s\n' "$output" | grep -F 'ParserIdentityComplete undefined' >/dev/null || {
  printf '%s\n' 'parser inspection identity RED failed for an unexpected reason'
  exit 2
}
printf '%s\n' 'RED: real parser inspection is not certified by the producer build identity'
exit 1
