package store

import (
	"database/sql"
	"path/filepath"
	"testing"
)

// ─────────────────────────────────────────────────────────────────────────────
// Content addressing (final_hardening item 2) -- the store half.
//
// The address is only useful if it survives to the reader, so it is stored the
// way every other per-symbol fact on `nodes` is stored: as columns, added
// through the same additive pragma_table_info/ALTER TABLE migration the store
// already runs, so an existing database gains the columns instead of being
// discarded. `byte_start`/`byte_end` already exist (callsites use them);
// `file_hash` is the one new column.
//
// The absent address must be NULL, not 0. A symbol from a graph built before
// this change is UNADDRESSED, and a reader that cannot distinguish that from a
// symbol addressed at byte 0 of an empty-hash file will happily "verify" it.
// ─────────────────────────────────────────────────────────────────────────────

func openGraph(t *testing.T) *DB {
	t.Helper()
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func readAddress(t *testing.T, db *DB, id int64) (sql.NullString, sql.NullInt64, sql.NullInt64) {
	t.Helper()
	var hash sql.NullString
	var start, end sql.NullInt64
	if err := db.db.QueryRow(`SELECT file_hash, byte_start, byte_end FROM nodes WHERE id = ?`, id).
		Scan(&hash, &start, &end); err != nil {
		t.Fatalf("read address: %v", err)
	}
	return hash, start, end
}

// The column has to exist on a freshly opened database and on one opened from
// an older file; the store's migration list is the single place that guarantees
// both, so the pin is on the schema the reader will actually query.
func TestNodesCarryAFileHashColumn(t *testing.T) {
	db := openGraph(t)
	for _, column := range []string{"file_hash", "byte_start", "byte_end"} {
		var count int
		if err := db.db.QueryRow(`SELECT count(*) FROM pragma_table_info('nodes') WHERE name=?`, column).Scan(&count); err != nil {
			t.Fatalf("inspect nodes.%s: %v", column, err)
		}
		if count != 1 {
			t.Errorf("nodes.%s is missing -- a symbol address cannot be stored", column)
		}
	}
}

func TestBatchInsertNodesPersistsTheAddress(t *testing.T) {
	db := openGraph(t)
	ids, err := db.BatchInsertNodes([]*Node{{
		Label: "Function", Name: "fetch", QualifiedName: "Repo.fetch",
		FilePath: "repo.py", StartLine: 2, EndLine: 3, Language: "python",
		FileHash: "9f2c1a", ByteStart: 12, ByteEnd: 61,
	}})
	if err != nil {
		t.Fatalf("insert: %v", err)
	}
	hash, start, end := readAddress(t, db, ids[0])
	if hash.String != "9f2c1a" || start.Int64 != 12 || end.Int64 != 61 {
		t.Errorf("stored address = (%v,%v,%v), want (9f2c1a,12,61)", hash, start, end)
	}
}

// A graph built before content addressing must read back as UNADDRESSED. Zero
// is a legal byte offset, so writing 0 would make an old symbol indistinguish-
// able from one that genuinely starts at the top of its file.
func TestUnaddressedSymbolStoresNullNotZero(t *testing.T) {
	db := openGraph(t)
	ids, err := db.BatchInsertNodes([]*Node{{
		Label: "Function", Name: "legacy", QualifiedName: "legacy",
		FilePath: "old.py", StartLine: 1, EndLine: 1, Language: "python",
	}})
	if err != nil {
		t.Fatalf("insert: %v", err)
	}
	hash, start, end := readAddress(t, db, ids[0])
	if hash.Valid || start.Valid || end.Valid {
		t.Errorf("unaddressed symbol stored (%v,%v,%v), want all NULL", hash, start, end)
	}
}

// Incremental reindex writes through its own transactional insert. A symbol
// re-indexed after a file edit that lost its address would be delivered
// unverified, so the two insert paths must agree.
func TestBatchInsertNodesTxPersistsTheAddress(t *testing.T) {
	db := openGraph(t)
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	ids, err := BatchInsertNodesTx(tx, []*Node{{
		Label: "Method", Name: "handle", QualifiedName: "App.handle",
		FilePath: "app.go", StartLine: 4, EndLine: 6, Language: "go",
		FileHash: "aa01ff", ByteStart: 40, ByteEnd: 90,
	}})
	if err != nil {
		_ = tx.Rollback()
		t.Fatalf("insert tx: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("commit: %v", err)
	}
	hash, start, end := readAddress(t, db, ids[0])
	if hash.String != "aa01ff" || start.Int64 != 40 || end.Int64 != 90 {
		t.Errorf("stored address = (%v,%v,%v), want (aa01ff,40,90)", hash, start, end)
	}
}
