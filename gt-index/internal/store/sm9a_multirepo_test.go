package store

import (
	"database/sql"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// SM-9a MULTI-REPO — schema partition key + repos table + stamping.
//
// The load-bearing GUARD is single-root BYTE-IDENTITY: a single-repo index (which
// never sets repo_id) must leave every repo_id NULL and the repos table empty, so
// every existing query that omits repo_id behaves identically to the pre-multi-repo
// binary. TestSM9a_BackCompat_SingleRootInsertsLeaveRepoIDNull is that golden —
// breaking the default repo_id (e.g. a NOT NULL / DEFAULT 0 column) reddens it.

func tblCols(t *testing.T, d *DB, table string) map[string]bool {
	t.Helper()
	rows, err := d.db.Query("PRAGMA table_info(" + table + ")")
	if err != nil {
		t.Fatalf("table_info(%s): %v", table, err)
	}
	defer rows.Close()
	cols := map[string]bool{}
	for rows.Next() {
		var cid, notnull, pk int
		var name, ctype string
		var dflt sql.NullString
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			t.Fatal(err)
		}
		cols[name] = true
	}
	return cols
}

// A fresh schema must carry repo_id on every fact surface + the repos table.
func TestSM9a_RepoIDColumnsAndReposTablePresent(t *testing.T) {
	d := b22TestDB(t)
	for _, table := range []string{"nodes", "edges", "properties", "assertions", "closure", "content_passages"} {
		if !tblCols(t, d, table)["repo_id"] {
			t.Errorf("table %s is missing the repo_id partition column", table)
		}
	}
	var n int
	if err := d.db.QueryRow("SELECT COUNT(*) FROM repos").Scan(&n); err != nil {
		t.Fatalf("repos table not present/queryable: %v", err)
	}
	if n != 0 {
		t.Errorf("fresh repos table rowcount=%d want 0", n)
	}
}

// BACK-COMPAT GOLDEN: the single-root write path uses BatchInsertNodes/BatchInsertEdges
// with NO repo_id in the column list, so repo_id must stay NULL. If the default is
// broken (column becomes NOT NULL / DEFAULT 0, or an insert stamps it), this reddens.
func TestSM9a_BackCompat_SingleRootInsertsLeaveRepoIDNull(t *testing.T) {
	d := b22TestDB(t)
	ids, err := d.BatchInsertNodes([]*Node{
		{Label: "Function", Name: "a", FilePath: "x.go", Language: "go"},
		{Label: "Function", Name: "b", FilePath: "x.go", Language: "go"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := d.BatchInsertEdges([]*Edge{
		{SourceID: ids[0], TargetID: ids[1], Type: "CALLS", ResolutionMethod: "same_file", Confidence: 1.0},
	}); err != nil {
		t.Fatal(err)
	}
	if err := d.BatchInsertProperties([]*Property{
		{NodeID: ids[0], Kind: "visibility", Value: "unexported", Line: 1, Confidence: 1.0},
	}); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct{ table, col string }{
		{"nodes", "repo_id"}, {"edges", "repo_id"}, {"properties", "repo_id"},
	} {
		var nonNull int
		if err := d.db.QueryRow(
			"SELECT COUNT(*) FROM " + tc.table + " WHERE " + tc.col + " IS NOT NULL",
		).Scan(&nonNull); err != nil {
			t.Fatal(err)
		}
		if nonNull != 0 {
			t.Errorf("BACK-COMPAT BROKEN: %s.%s populated on a single-root insert (%d non-NULL rows); "+
				"the single-root path must leave repo_id NULL for byte-identity", tc.table, tc.col, nonNull)
		}
	}
	if got := d.RepoCount(); got != 0 {
		t.Errorf("single-root RepoCount=%d want 0 (no repos row on the single-root path)", got)
	}
}

// migrateSchema must ADD repo_id to a fact table created by an OLDER binary (idempotent),
// so an old single-root db reopens with repo_id present-and-NULL. Removing a table from
// migrateSchema's repo_id loop reddens this for that table.
func TestSM9a_MigrateAddsRepoIDToOldDB(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "old.db")
	raw, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()
	// Legacy tables WITHOUT repo_id (a pre-SM-9a binary's shape). Minimal columns.
	legacy := []string{
		`CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT, language TEXT)`,
		`CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT)`,
		`CREATE TABLE properties (id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT, line INTEGER, confidence REAL)`,
		`CREATE TABLE assertions (id INTEGER PRIMARY KEY, test_node_id INTEGER, kind TEXT, expression TEXT)`,
		`CREATE TABLE closure (source_id INTEGER, target_id INTEGER, depth INTEGER)`,
		`CREATE TABLE content_passages (passage_id INTEGER PRIMARY KEY, node_id INTEGER, start_line INTEGER, end_line INTEGER, content TEXT, content_hash TEXT)`,
	}
	for _, ddl := range legacy {
		if _, err := raw.Exec(ddl); err != nil {
			t.Fatalf("legacy ddl: %v", err)
		}
	}
	for _, table := range []string{"nodes", "edges", "properties", "assertions", "closure", "content_passages"} {
		if colExists(t, raw, table, "repo_id") {
			t.Fatalf("precondition: legacy %s already has repo_id", table)
		}
	}
	if err := migrateSchema(raw); err != nil {
		t.Fatalf("migrateSchema: %v", err)
	}
	for _, table := range []string{"nodes", "edges", "properties", "assertions", "closure", "content_passages"} {
		if !colExists(t, raw, table, "repo_id") {
			t.Errorf("migrateSchema did not add repo_id to legacy %s", table)
		}
	}
	// Idempotent: a second migrate is a no-op (no error).
	if err := migrateSchema(raw); err != nil {
		t.Fatalf("migrateSchema (second call not idempotent): %v", err)
	}
}

func colExists(t *testing.T, raw *sql.DB, table, col string) bool {
	t.Helper()
	rows, err := raw.Query("PRAGMA table_info(" + table + ")")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	for rows.Next() {
		var cid, notnull, pk int
		var name, ctype string
		var dflt sql.NullString
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			t.Fatal(err)
		}
		if name == col {
			return true
		}
	}
	return false
}

// UpsertRepo / StampNodeRepoIDs / StampDerivedRepoIDs / CrossRepoNodeRows on the
// multi-repo path: two repos with a COLLIDING relative path get disambiguated by
// repo_id, and derived surfaces inherit their node's repo_id by GLOBAL node id.
func TestSM9a_StampAndDerive(t *testing.T) {
	d := b22TestDB(t)
	if err := d.UpsertRepo(0, "/repoA", "aaa"); err != nil {
		t.Fatal(err)
	}
	if err := d.UpsertRepo(1, "/repoB", "bbb"); err != nil {
		t.Fatal(err)
	}
	if got := d.RepoCount(); got != 2 {
		t.Fatalf("RepoCount=%d want 2", got)
	}
	// Same relative path in both repos (the collision the schema note warns about).
	a, err := d.BatchInsertNodes([]*Node{{Label: "Function", Name: "helper", FilePath: "util.go", Language: "go", IsExported: true}})
	if err != nil {
		t.Fatal(err)
	}
	b, err := d.BatchInsertNodes([]*Node{{Label: "Function", Name: "helper", FilePath: "util.go", Language: "go", IsExported: true}})
	if err != nil {
		t.Fatal(err)
	}
	if err := d.StampNodeRepoIDs(a, 0); err != nil {
		t.Fatal(err)
	}
	if err := d.StampNodeRepoIDs(b, 1); err != nil {
		t.Fatal(err)
	}
	// An edge A.helper -> B.helper (cross-repo); a property on A.helper.
	if err := d.BatchInsertEdges([]*Edge{{SourceID: a[0], TargetID: b[0], Type: "CALLS", ResolutionMethod: "call_cross_repo", Confidence: 0.95}}); err != nil {
		t.Fatal(err)
	}
	if err := d.BatchInsertProperties([]*Property{{NodeID: b[0], Kind: "visibility", Value: "exported", Line: 1, Confidence: 1.0}}); err != nil {
		t.Fatal(err)
	}
	if err := d.StampDerivedRepoIDs(); err != nil {
		t.Fatal(err)
	}
	// Edge repo_id = SOURCE repo (0); property repo_id = its node's repo (1).
	var edgeRepo, propRepo int64
	if err := d.db.QueryRow("SELECT repo_id FROM edges WHERE source_id=? AND target_id=?", a[0], b[0]).Scan(&edgeRepo); err != nil {
		t.Fatal(err)
	}
	if edgeRepo != 0 {
		t.Errorf("cross-repo edge repo_id=%d want 0 (source repo)", edgeRepo)
	}
	if err := d.db.QueryRow("SELECT repo_id FROM properties WHERE node_id=?", b[0]).Scan(&propRepo); err != nil {
		t.Fatal(err)
	}
	if propRepo != 1 {
		t.Errorf("property repo_id=%d want 1 (owning node's repo)", propRepo)
	}
	// CrossRepoNodeRows returns both exported nodes, each with its correct repo.
	rows, err := d.CrossRepoNodeRows()
	if err != nil {
		t.Fatal(err)
	}
	got := map[int64]int64{}
	for _, r := range rows {
		got[r.ID] = r.RepoID
	}
	if got[a[0]] != 0 || got[b[0]] != 1 {
		t.Errorf("CrossRepoNodeRows repo partition wrong: A=%d(want0) B=%d(want1)", got[a[0]], got[b[0]])
	}
}
