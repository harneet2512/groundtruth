#!/bin/sh
set -eu

if grep -Fq 'tx.Exec("DELETE FROM " + table)' gt-index/internal/store/phase_receipt.go ||
   grep -Fq 'db.Exec(`ALTER TABLE edges ADD COLUMN ` + column.name' gt-index/internal/store/sqlite.go ||
   grep -Fq 'db.Exec(`ALTER TABLE cochanges ADD COLUMN ` + column.name' gt-index/internal/store/sqlite.go; then
  echo "RED: schema maintenance concatenates SQL identifiers"
  exit 1
fi

echo "schema maintenance uses enumerated SQL constants"
