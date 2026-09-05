#!/bin/sh
set -eu

if [ ! -f gt-index/cmd/gt-index/inspection.go ]; then
  echo "RED: deterministic inspection transport is absent"
  exit 1
fi
if ! grep -Fq 'case "inspect-source":' gt-index/cmd/gt-index/main.go; then
  echo "RED: deterministic inspection transport is not wired"
  exit 1
fi
if grep -Fq 'INSERT INTO resolution_candidates' gt-index/internal/store/resolution_stmts.go; then
  echo "RED: legacy resolution candidate duplication remains writable"
  exit 1
fi
if ! grep -Fq 'CANDIDATE_TARGET' gt-index/internal/store/resolution_stmts.go; then
  echo "RED: canonical candidate target edge is absent"
  exit 1
fi

echo "deterministic inspection and canonical candidate storage are present"
