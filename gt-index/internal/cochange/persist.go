package cochange

import (
	"database/sql"
	"fmt"
)

// CochangesColumns names the columns Persist writes, in schema order:
//
//	file_a TEXT NOT NULL
//	file_b TEXT NOT NULL
//	count INTEGER NOT NULL DEFAULT 1
//	commits_a INTEGER NOT NULL DEFAULT 0
//	commits_b INTEGER NOT NULL DEFAULT 0
//	confidence_a_to_b REAL NOT NULL DEFAULT 0.0
//	confidence_b_to_a REAL NOT NULL DEFAULT 0.0
//	PRIMARY KEY(file_a, file_b)
//
// Naming this list makes schema drift visible beside the persistence code.
const CochangesColumns = "file_a, file_b, count, commits_a, commits_b, confidence_a_to_b, confidence_b_to_a"

// insertCochangeSQL is idempotent under the (file_a, file_b) primary key.
const insertCochangeSQL = "INSERT OR REPLACE INTO cochanges (" + CochangesColumns + ") VALUES (?, ?, ?, ?, ?, ?, ?)"

// Persist writes result.Pairs into the existing cochanges table inside the
// caller's transaction and returns the number of rows written. Both directional
// denominators and confidences are retained. Window boundaries and abstention
// reasons stay in phase-level receipt metadata because they describe the run,
// not an individual pair.
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
	// Result.Pairs is sorted by (FileA, FileB), keeping writes deterministic.
	for _, p := range result.Pairs {
		if _, err := stmt.Exec(
			p.FileA, p.FileB, p.Support, p.CommitsA, p.CommitsB,
			p.ConfidenceAToB, p.ConfidenceBToA,
		); err != nil {
			return written, fmt.Errorf("cochange: insert pair (%s, %s): %w", p.FileA, p.FileB, err)
		}
		written++
	}
	return written, nil
}
