#!/bin/sh
set -eu

if grep -R "func AnalyzeCHAThenRTA" gt-index/internal/resolver >/dev/null 2>&1; then
  printf '%s\n' 'CHA/RTA implementation unexpectedly present'
  exit 0
fi
printf '%s\n' 'CHA/RTA implementation missing'
exit 1
