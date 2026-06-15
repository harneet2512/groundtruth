package main

import (
	"database/sql"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// G09 (MEDIUM, integration): the class inheritanceMap (consumed by
// lookupMethodWithInheritance for the CHA rungs — gt_gt §2.3) was built ONLY on
// the full-index path (main.go:426-428). On the incremental `-file` path it was
// never set → nil → a cross-file inherited-method call under-resolved to name_match
// on every `gt-index -file` reindex.
//
// This is a true END-TO-END red→green of the WIRING: it builds the gt-index binary,
// full-indexes a 2-file Python repo where Child(Base) lives in a different file than
// Base and calls an INHERITED method (self.save → Base.save), then reindexes the
// child with `-file` and asserts the inherited call edge still resolves via the
// inheritance chain (resolution_method NOT name_match), with the receiving node in
// base.py. Before the fix the `-file` reindex left inheritanceMap nil → the self.save
// edge demoted to name_match (or dropped). After the fix the reconstructed
// whole-graph inheritance map keeps it resolved across the reindex.
//
// The full-index path is only reachable through main(), so the test drives the
// compiled binary (the cluster's build command), not an internal entrypoint.
func TestIncrementalReindexPreservesInheritedMethodResolution(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}

	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(filepath.Join(repo, "pkg"), 0o755); err != nil {
		t.Fatal(err)
	}

	baseRel := "pkg/base.py"
	childRel := "pkg/child.py"
	// Base defines save(); Child(Base) inherits it and CALLS it via self.save().
	// The self.save() call only resolves cross-file through the inheritance chain.
	if err := os.WriteFile(filepath.Join(repo, baseRel),
		[]byte("class Base:\n    def save(self):\n        return 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, childRel),
		[]byte("from pkg.base import Base\n\n\nclass Child(Base):\n    def run(self):\n        return self.save()\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	// Build the gt-index binary from this module.
	bin := filepath.Join(tmp, "gt-index")
	if runtime.GOOS == "windows" {
		bin += ".exe"
	}
	build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-o", bin, ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=1")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build gt-index: %v\n%s", err, out)
	}

	dbPath := filepath.Join(tmp, "graph.db")

	// Pass 1: FULL index (this is the only path that built inheritanceMap pre-fix).
	full := exec.Command(bin, "-root", repo, "-output", dbPath)
	if out, err := full.CombinedOutput(); err != nil {
		t.Fatalf("full index: %v\n%s", err, out)
	}

	// Sanity: the inherited self.save() edge resolved on the full path.
	if m := inheritedSaveEdgeMethod(t, dbPath); m == "" {
		t.Fatalf("full index did not produce a self.save() -> Base.save edge at all (test fixture invalid)")
	} else if m == "name_match" {
		t.Fatalf("full index resolved self.save() as name_match (expected an inheritance-chain method); fixture/resolver mismatch")
	}

	// Pass 2: INCREMENTAL reindex of the CHILD file. Pre-fix this rebuilt the child's
	// calls with inheritanceMap == nil, so the self.save() edge demoted to name_match
	// (or dropped). Post-fix the reconstructed whole-graph inheritance map keeps it
	// resolved through the chain.
	incr := exec.Command(bin, "-root", repo, "-output", dbPath, "-file", childRel)
	if out, err := incr.CombinedOutput(); err != nil {
		t.Fatalf("incremental reindex: %v\n%s", err, out)
	}

	method := inheritedSaveEdgeMethod(t, dbPath)
	if method == "" {
		t.Fatalf("after -file reindex the self.save() -> Base.save edge is GONE (inheritance chain lost — the G09 regression)")
	}
	if method == "name_match" {
		t.Fatalf("after -file reindex self.save() demoted to name_match (inheritanceMap was nil on the incremental path — the G09 regression)")
	}
}

// inheritedSaveEdgeMethod returns the resolution_method of the CALLS edge from a
// node in child.py to the `save` node in base.py (the inherited-method call), or
// "" if no such edge exists.
func inheritedSaveEdgeMethod(t *testing.T, dbPath string) string {
	t.Helper()
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	var method string
	err = db.QueryRow(
		`SELECT COALESCE(e.resolution_method, '')
		   FROM edges e
		   JOIN nodes src ON e.source_id = src.id
		   JOIN nodes tgt ON e.target_id = tgt.id
		  WHERE e.type = 'CALLS'
		    AND tgt.name = 'save'
		    AND tgt.file_path LIKE '%base.py'
		    AND src.file_path LIKE '%child.py'
		  ORDER BY e.id
		  LIMIT 1`,
	).Scan(&method)
	if err == sql.ErrNoRows {
		return ""
	}
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(method)
}
