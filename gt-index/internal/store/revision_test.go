package store

import (
	"database/sql"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// revTestDB opens a fresh schema'd DB wrapped as *store.DB.
func revTestDB(t *testing.T) *DB {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	sqldb, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := createSchema(sqldb); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqldb.Close() })
	return &DB{db: sqldb}
}

func revSeedTwoNodes(t *testing.T, d *DB) {
	t.Helper()
	if _, err := d.db.Exec(
		`INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, signature, language) VALUES
		 (1, 'Function', 'caller', 'pkg.caller', 'src/a.py', 1, 3, 'def caller()', 'python'),
		 (2, 'Function', 'callee', 'pkg.callee', 'src/b.py', 1, 3, 'def callee()', 'python')`,
	); err != nil {
		t.Fatal(err)
	}
}

func revInsertEdge(t *testing.T, d *DB, src, tgt int64, method string, conf float64) {
	t.Helper()
	if _, err := d.db.Exec(
		`INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence, trust_tier, candidate_count, evidence_type, verification_status)
		 VALUES (?, ?, 'CALLS', 5, 'src/a.py', ?, ?, 'CERTIFIED', 1, 'ast_call', 'unverified')`,
		src, tgt, method, conf,
	); err != nil {
		t.Fatal(err)
	}
}

// TestComputeRevisionDeterministic — two computations over the SAME db state
// return byte-identical revisions (invariant 1: two identical -file runs ->
// identical post_revision). Re-computing must be a pure function of content.
func TestComputeRevisionDeterministic(t *testing.T) {
	d := revTestDB(t)
	revSeedTwoNodes(t, d)
	revInsertEdge(t, d, 1, 2, "import", 1.0)

	r1, err := d.ComputeRevision()
	if err != nil {
		t.Fatal(err)
	}
	r2, err := d.ComputeRevision()
	if err != nil {
		t.Fatal(err)
	}
	if r1 != r2 {
		t.Fatalf("ComputeRevision not deterministic on identical state: %q != %q", r1, r2)
	}
	if len(r1) != 64 {
		t.Fatalf("revision is not a hex sha256 (len=%d): %q", len(r1), r1)
	}
}

// TestComputeRevisionDetectsEdgeChange — the M1-guard. Adding an EDGE while the
// node set is byte-identical MUST change the revision. If ComputeRevision ever
// stops hashing edges (mutation M1), this determinism-detects-change assertion
// bites (r1 == r2).
func TestComputeRevisionDetectsEdgeChange(t *testing.T) {
	d := revTestDB(t)
	revSeedTwoNodes(t, d)
	revInsertEdge(t, d, 1, 2, "import", 1.0)
	r1, err := d.ComputeRevision()
	if err != nil {
		t.Fatal(err)
	}

	// Add a SECOND edge — nodes are unchanged, only the edge set differs.
	revInsertEdge(t, d, 2, 1, "same_file", 1.0)
	r2, err := d.ComputeRevision()
	if err != nil {
		t.Fatal(err)
	}
	if r1 == r2 {
		t.Fatalf("edge-only change did not move the revision (edges not hashed?): %q", r1)
	}
}

// TestComputeRevisionDetectsEdgeAttributeChange — a change to an edge COLUMN
// (confidence/method) with the same endpoints must move the revision. Also
// guards M1 (edges hashed) at attribute granularity.
func TestComputeRevisionDetectsEdgeAttributeChange(t *testing.T) {
	d := revTestDB(t)
	revSeedTwoNodes(t, d)
	revInsertEdge(t, d, 1, 2, "import", 1.0)
	r1, _ := d.ComputeRevision()

	if _, err := d.db.Exec(`UPDATE edges SET confidence = 0.2, resolution_method = 'name_match' WHERE source_id = 1`); err != nil {
		t.Fatal(err)
	}
	r2, _ := d.ComputeRevision()
	if r1 == r2 {
		t.Fatalf("edge attribute change (conf/method) did not move the revision: %q", r1)
	}
}

// TestComputeRevisionDetectsNodeChange — a change to a NODE column (signature)
// must move the revision (guards that nodes are hashed).
func TestComputeRevisionDetectsNodeChange(t *testing.T) {
	d := revTestDB(t)
	revSeedTwoNodes(t, d)
	revInsertEdge(t, d, 1, 2, "import", 1.0)
	r1, _ := d.ComputeRevision()

	if _, err := d.db.Exec(`UPDATE nodes SET signature = 'def callee(x, y)' WHERE id = 2`); err != nil {
		t.Fatal(err)
	}
	r2, _ := d.ComputeRevision()
	if r1 == r2 {
		t.Fatalf("node signature change did not move the revision: %q", r1)
	}
}

// TestComputeRevisionExcludesProjectMeta — writing project_meta (where
// post_revision itself lives, alongside build stamps) must NOT change the
// revision. This is the no-self-reference invariant: stamping post_revision
// into project_meta cannot perturb the value it just computed.
func TestComputeRevisionExcludesProjectMeta(t *testing.T) {
	d := revTestDB(t)
	revSeedTwoNodes(t, d)
	revInsertEdge(t, d, 1, 2, "import", 1.0)
	r1, _ := d.ComputeRevision()

	if err := d.SetMeta("post_revision", "deadbeef"); err != nil {
		t.Fatal(err)
	}
	if err := d.SetMeta("build_time_utc", "2026-07-10T00:00:00Z"); err != nil {
		t.Fatal(err)
	}
	r2, _ := d.ComputeRevision()
	if r1 != r2 {
		t.Fatalf("project_meta writes perturbed the revision (self-reference): %q != %q", r1, r2)
	}
}

// TestComputeRevisionIdIndependent — the same logical graph inserted under
// DIFFERENT surrogate ids yields the SAME revision. Guards that endpoints are
// hashed by node identity, not raw source_id/target_id (so a full rebuild and an
// incremental delete-and-replace of the same source converge).
func TestComputeRevisionIdIndependent(t *testing.T) {
	d1 := revTestDB(t)
	if _, err := d1.db.Exec(
		`INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, signature, language) VALUES
		 (1, 'Function', 'caller', 'pkg.caller', 'src/a.py', 1, 3, 'def caller()', 'python'),
		 (2, 'Function', 'callee', 'pkg.callee', 'src/b.py', 1, 3, 'def callee()', 'python')`,
	); err != nil {
		t.Fatal(err)
	}
	revInsertEdge(t, d1, 1, 2, "import", 1.0)
	r1, _ := d1.ComputeRevision()

	// Same two nodes + same logical edge, but under ids 101/102 (as a
	// delete-and-replace reindex would assign via AUTOINCREMENT).
	d2 := revTestDB(t)
	if _, err := d2.db.Exec(
		`INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, signature, language) VALUES
		 (101, 'Function', 'caller', 'pkg.caller', 'src/a.py', 1, 3, 'def caller()', 'python'),
		 (102, 'Function', 'callee', 'pkg.callee', 'src/b.py', 1, 3, 'def callee()', 'python')`,
	); err != nil {
		t.Fatal(err)
	}
	revInsertEdge(t, d2, 101, 102, "import", 1.0)
	r2, _ := d2.ComputeRevision()

	if r1 != r2 {
		t.Fatalf("revision depends on surrogate ids (not identity-hashed): %q != %q", r1, r2)
	}
}

// TestGetMetaRoundTrip — GetMeta returns what SetMeta stored, "" when absent.
func TestGetMetaRoundTrip(t *testing.T) {
	d := revTestDB(t)
	if got := d.GetMeta("post_revision"); got != "" {
		t.Fatalf("absent key should be empty, got %q", got)
	}
	if err := d.SetMeta("post_revision", "abc123"); err != nil {
		t.Fatal(err)
	}
	if got := d.GetMeta("post_revision"); got != "abc123" {
		t.Fatalf("GetMeta=%q want abc123", got)
	}
}
