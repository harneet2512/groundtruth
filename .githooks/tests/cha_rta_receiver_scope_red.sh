#!/bin/sh
set -eu

if grep -R "declaredReceiverType" gt-index/internal/resolver >/dev/null 2>&1; then
  printf '%s\n' 'receiver-scoped CHA repair unexpectedly present'
  exit 0
fi
printf '%s\n' 'receiver-scoped CHA repair missing'
exit 1
