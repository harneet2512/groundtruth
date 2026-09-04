#!/bin/sh
set -eu

for edge in ACCESSES INJECTS METHOD_OVERRIDES; do
  if ! grep -Fq "= \"$edge\"" gt-index/internal/specs/taxonomy.go; then
    echo "RED: required taxonomy edge $edge is absent"
    exit 1
  fi
done

echo "required taxonomy edge interfaces are declared"
