package process

import (
	"context"
	"database/sql"
	"fmt"
)

// Run is the single call the indexer makes: open the graph at dbPath, create
// the process tables if absent, derive every test-witnessed process, and
// persist them in one transaction. The Result is returned so the caller can
// log it and seal the counts into the index receipt.
//
// It opens its own connection rather than taking the indexer's *store.DB
// because store.DB does not expose its underlying *sql.DB, and adding an
// accessor would mean editing the protected core-graph store. A second SQLite
// connection to the same file is safe: the derivation is read-only until the
// final transaction, and that transaction touches only tables this package
// owns.
//
// Failure is the caller's to classify. The process tables are an analysis
// sidecar, not core graph, so a failure here should degrade the index (no
// processes, recorded as absent) rather than abort it — the same contract the
// closure sidecar already has, and the seam final_hardening item 9 splits.
func Run(ctx context.Context, dbPath string, opts Options) (Result, error) {
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		return Result{}, fmt.Errorf("open %s for process derivation: %w", dbPath, err)
	}
	defer db.Close()

	if err := EnsureSchema(db); err != nil {
		return Result{}, err
	}

	res, err := Derive(ctx, db, opts)
	if err != nil {
		return Result{}, err
	}

	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return res, fmt.Errorf("begin process publication: %w", err)
	}
	if err := Persist(tx, res); err != nil {
		_ = tx.Rollback()
		return res, err
	}
	if err := tx.Commit(); err != nil {
		return res, fmt.Errorf("commit process publication: %w", err)
	}
	return res, nil
}
