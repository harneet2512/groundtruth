package main

import (
	"bytes"
	"database/sql"
	"os/exec"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// Graph-F1 (bounce 2026-07-10): the `-rebuild-closure` pass runs AFTER the LSP resolve
// pass (groundtruth.resolve) UPDATE/DELETEs edges + UPDATEs nodes, but it used to run
// ClearClosure + closure + PopulateFTS5 + CheckpointWAL and NEVER re-stamp the composite
// revision. So project_meta.post_revision (and every subrev_<surface>) kept fingerprinting
// the PRE-LSP graph — the B-11 live-read revision, the B-21 latch re-permit, and the
// incremental "did the graph change" contract were all keyed to a superseded stamp.
//
// This test full-indexes, MUTATES the graph the way the LSP resolve pass does (delete an
// edge), runs `-rebuild-closure`, and asserts post_revision (and subrev_edges) MOVED. Pre-
// fix they do not move (RED); after adding PopulateEdgeMetadata + StampCompositeRevision to
// the rebuild-closure pass they do (GREEN).

func readMeta(t *testing.T, db, key string) string {
	t.Helper()
	conn, err := sql.Open("sqlite3", db)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	defer conn.Close()
	var v string
	if err := conn.QueryRow("SELECT value FROM project_meta WHERE key = ?", key).Scan(&v); err != nil {
		t.Fatalf("read project_meta[%s]: %v", key, err)
	}
	return v
}

// mutateDeleteOneEdge simulates the LSP resolve pass's edge mutation: delete one CALLS
// edge (resolve.py DELETEs edges it disproves). Returns after the write is committed.
func mutateDeleteOneEdge(t *testing.T, db string) {
	t.Helper()
	conn, err := sql.Open("sqlite3", db)
	if err != nil {
		t.Fatalf("open db for mutate: %v", err)
	}
	defer conn.Close()
	res, err := conn.Exec("DELETE FROM edges WHERE id = (SELECT MIN(id) FROM edges WHERE type='CALLS')")
	if err != nil {
		t.Fatalf("delete edge: %v", err)
	}
	n, _ := res.RowsAffected()
	if n != 1 {
		t.Fatalf("expected to delete exactly 1 CALLS edge, deleted %d", n)
	}
}

func runRebuildClosure(t *testing.T, bin, db string) {
	t.Helper()
	cmd := exec.Command(bin, "-output", db, "-rebuild-closure")
	var o, e bytes.Buffer
	cmd.Stdout = &o
	cmd.Stderr = &e
	if err := cmd.Run(); err != nil {
		t.Fatalf("rebuild-closure failed: %v\nstderr=%s", err, e.String())
	}
}

func TestRebuildClosureRestampsCompositeRevision(t *testing.T) {
	bin := buildGTIndex(t)
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	db := filepath.Join(tmp, "graph.db")
	// pyC: foo; bar->foo; baz->bar  => at least two CALLS edges to delete from.
	writeFile(t, filepath.Join(repo, "pkg/mod.py"), pyC)
	fullIndex(t, bin, repo, db)

	rev0 := readMeta(t, db, "post_revision")
	edges0 := readMeta(t, db, "subrev_edges")
	if len(rev0) != 64 {
		t.Fatalf("post_revision after full index is not a hex sha256: %q", rev0)
	}

	// Simulate the LSP resolve pass mutating edges, THEN the closure rebuild that must
	// re-fingerprint the post-LSP graph.
	mutateDeleteOneEdge(t, db)
	runRebuildClosure(t, bin, db)

	rev1 := readMeta(t, db, "post_revision")
	edges1 := readMeta(t, db, "subrev_edges")

	if rev1 == rev0 {
		t.Errorf("rebuild-closure did NOT re-stamp post_revision after an edge mutation: still %q "+
			"(the live revision fingerprints the PRE-LSP graph)", rev0)
	}
	if edges1 == edges0 {
		t.Errorf("rebuild-closure did NOT re-stamp subrev_edges after deleting an edge: still %q", edges0)
	}
	if len(rev1) != 64 {
		t.Errorf("re-stamped post_revision is not a hex sha256: %q", rev1)
	}
}

// TestRebuildClosureNoopRevisionStable — a rebuild-closure over an UNMUTATED graph must
// re-stamp to the SAME revision (deterministic content hash), never churn it. Guards the
// fix from introducing nondeterminism into the stamp.
func TestRebuildClosureNoopRevisionStable(t *testing.T) {
	bin := buildGTIndex(t)
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	db := filepath.Join(tmp, "graph.db")
	writeFile(t, filepath.Join(repo, "pkg/mod.py"), pyC)
	fullIndex(t, bin, repo, db)

	rev0 := readMeta(t, db, "post_revision")
	runRebuildClosure(t, bin, db) // no mutation
	rev1 := readMeta(t, db, "post_revision")
	if rev1 != rev0 {
		t.Errorf("rebuild-closure over an unchanged graph churned post_revision: %q -> %q", rev0, rev1)
	}
}
