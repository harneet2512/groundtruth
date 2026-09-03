package cochange

import (
	"database/sql"
	"fmt"
)

// CochangesColumns names the columns Persist writes, in the order the schema
// declares them (internal/store/sqlite.go, `CREATE TABLE cochanges`):
//
//	file_a TEXT NOT NULL
//	file_b TEXT NOT NULL
//	count  INTEGER NOT NULL DEFAULT 1
//	PRIMARY KEY(file_a, file_b)
//
// It is a named constant so a schema drift shows up as a compile-adjacent
// review diff rather than as a runtime "no such column" during publication.
const CochangesColumns = "file_a, file_b, count"

// insertCochangeSQL is idempotent under the (file_a, file_b) primary key, so
// re-publishing a graph replaces a pair's count instead of failing on it.
const insertCochangeSQL = "INSERT OR REPLACE INTO cochanges (" + CochangesColumns + ") VALUES (?, ?, ?)"

// Persist writes result.Pairs into the existing `cochanges` table inside the
// caller's transaction, and returns the number of rows written.
//
// It takes the transaction rather than opening one so co-change lands in the
// same atomic publication as the rest of the graph: a graph that commits must
// not be able to commit without the coupling rows it reported.
//
// KNOWN LOSS, stated rather than hidden: `cochanges` has three columns and no
// place for a confidence, a window boundary or a reason. Persist therefore
// stores Support as `count` and DROPS ConfidenceAToB, ConfidenceBToA,
// CommitsA and CommitsB. Confidence is recoverable at query time only if the
// per-file commit counts are recorded elsewhere — today they are not. Carrying
// them requires additive columns in sqlite.go, which is outside this package's
// remit; the extractor computes them so that change is a schema edit and not a
// re-derivation.
//
// A Result with zero pairs writes nothing and returns 0 without error: an
// abstention (Reason shallow_clone, no_history, not_a_repository) must leave
// the table empty, and the Reason belongs in the receipt, not in a data row.
func Persist(tx *sql.Tx, result Result) (int, error) {
	if tx == nil {
		return 0, fmt.Errorf("cochange: persist: nil transaction")
	}
	if len(result.Pairs) == 0 {
		return 0, nil
	}
	stmt, err := tx.Prepare(insertCochangeSQL)
	if err != nil {
		return 0, fmt.Errorf("cochange: prepare cochanges insert: %w", err)
	}
	defer stmt.Close()

	written := 0
	// result.Pairs is already sorted by (FileA, FileB), so the write order is
	// deterministic and two publications of the same history produce the same
	// page layout.
	for _, p := range result.Pairs {
		if _, err := stmt.Exec(p.FileA, p.FileB, p.Support); err != nil {
			return written, fmt.Errorf("cochange: insert pair (%s, %s): %w", p.FileA, p.FileB, err)
		}
		written++
	}
	return written, nil
}
