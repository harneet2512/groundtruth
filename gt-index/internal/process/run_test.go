package process

import (
	"context"
	"database/sql"
	"path/filepath"
	"reflect"
	"testing"
)

// Run is the exact call the proposed main.go wiring makes, so exercising it
// here means the wiring diff in REPORT.md is tested code rather than a sketch.
func TestRunDerivesAndPersists(t *testing.T) {
	db := newFixture(t)

	var path string
	if err := db.QueryRow(`SELECT file FROM pragma_database_list WHERE name='main'`).Scan(&path); err != nil {
		t.Fatal(err)
	}
	if path == "" {
		t.Fatal("fixture is not file-backed")
	}
	want := derive(t, db, Options{})

	res, err := Run(context.Background(), path, Options{})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if !reflect.DeepEqual(res, want) {
		t.Fatalf("Run result differs from Derive:\n got %+v\nwant %+v", res, want)
	}

	loaded, err := Load(db)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if !reflect.DeepEqual(loaded, want.Processes) {
		t.Fatalf("persisted processes differ:\n got %+v\nwant %+v", loaded, want.Processes)
	}

	// Run creates its own schema, so it works on a graph that has never seen
	// the process tables before.
	fresh := filepath.Join(t.TempDir(), "fresh.db")
	bare, err := sql.Open("sqlite3", fresh)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := bare.Exec(fixtureSchema); err != nil {
		t.Fatal(err)
	}
	bare.Close()

	res, err = Run(context.Background(), fresh, Options{})
	if err != nil {
		t.Fatalf("Run on a bare graph: %v", err)
	}
	if len(res.Processes) != 0 || res.Reason != ReasonNoAssertions {
		t.Fatalf("bare graph: processes=%d reason=%q; want 0 / %q",
			len(res.Processes), res.Reason, ReasonNoAssertions)
	}
}
