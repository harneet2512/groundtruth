package cochange

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// schemaCochanges is copied verbatim from internal/store/sqlite.go so the test
// fails loudly if Persist and the real schema ever disagree on columns.
const schemaCochanges = `
	CREATE TABLE IF NOT EXISTS cochanges (
		file_a TEXT NOT NULL,
		file_b TEXT NOT NULL,
		count INTEGER NOT NULL DEFAULT 1,
		PRIMARY KEY(file_a, file_b)
	);
	CREATE INDEX IF NOT EXISTS idx_cochanges_a ON cochanges(file_a);
	CREATE INDEX IF NOT EXISTS idx_cochanges_b ON cochanges(file_b);
`

func openCochangeDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(schemaCochanges); err != nil {
		t.Fatalf("create schema: %v", err)
	}
	return db
}

type row struct {
	a, b  string
	count int
}

func readCochanges(t *testing.T, db *sql.DB) []row {
	t.Helper()
	rs, err := db.Query("SELECT file_a, file_b, count FROM cochanges ORDER BY file_a, file_b")
	if err != nil {
		t.Fatalf("query: %v", err)
	}
	defer rs.Close()
	var out []row
	for rs.Next() {
		var r row
		if err := rs.Scan(&r.a, &r.b, &r.count); err != nil {
			t.Fatalf("scan: %v", err)
		}
		out = append(out, r)
	}
	if err := rs.Err(); err != nil {
		t.Fatalf("rows: %v", err)
	}
	return out
}

func TestPersistWritesSupportIntoTheExistingColumns(t *testing.T) {
	db := openCochangeDB(t)
	res := Result{
		Reason: ReasonOK,
		Pairs: []Pair{
			{FileA: "a.go", FileB: "b.go", Support: 4, CommitsA: 5, CommitsB: 4, ConfidenceAToB: 0.8, ConfidenceBToA: 1},
			{FileA: "a.go", FileB: "c.go", Support: 1, CommitsA: 5, CommitsB: 1, ConfidenceAToB: 0.2, ConfidenceBToA: 1},
		},
	}

	tx, err := db.Begin()
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	n, err := Persist(tx, res)
	if err != nil {
		t.Fatalf("Persist: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if n != 2 {
		t.Errorf("Persist wrote %d rows, want 2", n)
	}

	got := readCochanges(t, db)
	want := []row{{"a.go", "b.go", 4}, {"a.go", "c.go", 1}}
	if len(got) != len(want) {
		t.Fatalf("rows = %+v, want %+v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("row %d = %+v, want %+v", i, got[i], want[i])
		}
	}
}

func TestPersistIsIdempotentOnRepublish(t *testing.T) {
	db := openCochangeDB(t)
	first := Result{Pairs: []Pair{{FileA: "a.go", FileB: "b.go", Support: 4}}}
	second := Result{Pairs: []Pair{{FileA: "a.go", FileB: "b.go", Support: 9}}}

	for _, res := range []Result{first, second} {
		tx, err := db.Begin()
		if err != nil {
			t.Fatalf("begin: %v", err)
		}
		if _, err := Persist(tx, res); err != nil {
			t.Fatalf("Persist: %v", err)
		}
		if err := tx.Commit(); err != nil {
			t.Fatalf("commit: %v", err)
		}
	}

	got := readCochanges(t, db)
	if len(got) != 1 || got[0].count != 9 {
		t.Fatalf("rows = %+v, want exactly one row with the latest count 9", got)
	}
}

// An abstention must leave the table empty rather than fabricate a row that
// says "no coupling" — the reason belongs in the receipt.
func TestPersistWritesNothingForAnAbstention(t *testing.T) {
	db := openCochangeDB(t)
	tx, err := db.Begin()
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	n, err := Persist(tx, Result{Reason: ReasonShallowClone})
	if err != nil {
		t.Fatalf("Persist: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if n != 0 {
		t.Errorf("Persist wrote %d rows, want 0", n)
	}
	if got := readCochanges(t, db); len(got) != 0 {
		t.Errorf("rows = %+v, want none", got)
	}
}

func TestPersistRejectsNilTransaction(t *testing.T) {
	if _, err := Persist(nil, Result{Pairs: []Pair{{FileA: "a", FileB: "b", Support: 1}}}); err == nil {
		t.Fatal("Persist(nil, ...) returned no error")
	}
}

// The end-to-end path: a real repository, a real transaction, real rows.
func TestExtractThenPersistRoundTrip(t *testing.T) {
	dir := scriptedRepo(t)
	res, err := Extract(context.Background(), dir, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}

	db := openCochangeDB(t)
	tx, err := db.Begin()
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	n, err := Persist(tx, res)
	if err != nil {
		t.Fatalf("Persist: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if n != len(res.Pairs) {
		t.Fatalf("Persist wrote %d rows for %d pairs", n, len(res.Pairs))
	}

	got := readCochanges(t, db)
	if len(got) != 1 || got[0] != (row{"a.go", "b.go", 4}) {
		t.Fatalf("rows = %+v, want [{a.go b.go 4}]", got)
	}
}
