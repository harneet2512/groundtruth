package main

import (
	"database/sql"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
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
	otherRel := "pkg/other.py"
	// Base defines save(); Child(Base) inherits it and CALLS it via self.save().
	// CRITICAL for the test to BITE: a SECOND, unrelated class (Other) also defines
	// save(). That makes `save` AMBIGUOUS (2 candidates across files), so the resolver's
	// unique-name rung CANNOT resolve self.save() on its own — only the class-hierarchy
	// (inheritance map) disambiguates Child.self.save -> Base.save specifically. Without
	// the ambiguity the name is globally unique and a uniqueness rung resolves it even
	// with inheritanceMap == nil, which is exactly why the prior single-`save` fixture
	// passed on disabled wiring (tautological). With ambiguity, nil inheritanceMap ->
	// name_match (cc>1) or a wrong-class target, both of which fail the assertions below.
	if err := os.WriteFile(filepath.Join(repo, baseRel),
		[]byte("class Base:\n    def save(self):\n        return 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, otherRel),
		[]byte("class Other:\n    def save(self):\n        return 2\n"), 0o644); err != nil {
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
	build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-ldflags", testBuildLDFlags, "-o", bin, ".")
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

	// CRITICAL for the test to BITE: the incremental `-file` path SHA-256 short-circuits
	// (main.go:843) when the file's hash is unchanged — so reindexing the byte-identical
	// child would no-op and leave the full-index edge untouched (the prior tautology: the
	// resolution path never ran). MODIFY child.py first (add a method) so the hash differs
	// and the incremental resolver ACTUALLY re-resolves self.save(). The self.save() call
	// is preserved, so the edge is rebuilt — with inheritanceMap on the path or not.
	if err := os.WriteFile(filepath.Join(repo, childRel),
		[]byte("from pkg.base import Base\n\n\nclass Child(Base):\n    def run(self):\n        return self.save()\n\n    def extra(self):\n        return 0\n"), 0o644); err != nil {
		t.Fatal(err)
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
	assertGraphResolutionIncomplete(t, dbPath)

	// The incremental transaction must not leave old sidecar authority beside the
	// changed graph. The legacy CALLS edge above remains available, while the new
	// graph-native authority is explicitly incomplete and hidden from the generic
	// edge query until a full rebuild restores a single revision.
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, table := range []string{"resolution_symbols", "resolution_callsites", "resolution_candidates"} {
		var count int
		if err := db.QueryRow("SELECT count(*) FROM " + table).Scan(&count); err != nil {
			t.Fatalf("%s after incremental: %v", table, err)
		}
		if count != 0 {
			t.Fatalf("stale %s rows survived incremental invalidation: %d", table, count)
		}
	}
	for query, label := range map[string]string{
		`SELECT count(*) FROM edges WHERE type IN ('HAS_CALLSITE','CANDIDATE')`: "attached resolution edges",
		`SELECT count(*) FROM nodes WHERE label='Callsite'`:                     "attached callsite nodes",
	} {
		var count int
		if err := db.QueryRow(query).Scan(&count); err != nil {
			t.Fatalf("count %s after incremental: %v", label, err)
		}
		if count != 0 {
			t.Fatalf("stale %s survived incremental invalidation: %d", label, count)
		}
	}
	graph, err := store.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer graph.Close()
	if _, err := graph.QueryAttachedCandidates("save"); err == nil {
		t.Fatal("incomplete incremental graph exposed attached candidate authority")
	}
	visible, err := graph.GetAllEdges(0)
	if err != nil {
		t.Fatal(err)
	}
	for _, edge := range visible {
		if edge.Type == "HAS_CALLSITE" || edge.Type == "CANDIDATE" {
			t.Fatalf("generic graph query exposed incomplete attached edge: %+v", edge)
		}
	}
}

func assertGraphResolutionIncomplete(t *testing.T, dbPath string) {
	t.Helper()
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var complete, revision string
	if err := db.QueryRow("SELECT value FROM project_meta WHERE key='graph_resolution_complete'").Scan(&complete); err != nil {
		t.Fatalf("graph completion metadata after incremental: %v", err)
	}
	if err := db.QueryRow("SELECT value FROM project_meta WHERE key='graph_resolution_revision'").Scan(&revision); err != nil {
		t.Fatalf("graph revision metadata after incremental: %v", err)
	}
	if complete != "0" || revision != "stale" {
		t.Fatalf("incremental graph authority was not fail-closed: complete=%q revision=%q", complete, revision)
	}
	var attached int
	if err := db.QueryRow("SELECT count(*) FROM edges WHERE type='HAS_CALLSITE'").Scan(&attached); err != nil {
		t.Fatal(err)
	}
	if attached != 0 {
		t.Fatalf("incremental update retained stale primary graph callsite attachments: %d", attached)
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
