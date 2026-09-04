#!/bin/sh
set -eu

if grep -q 'incremental_reindex_requires_full_analysis' gt-index/internal/store/phase_receipt.go; then
  echo "incremental analysis invalidation is present"
  exit 0
fi

echo "RED: incremental reindex leaves stale derived analysis eligible for publication"
exit 1
