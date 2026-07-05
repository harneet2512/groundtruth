package main

import (
	"database/sql"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// B1 (Fable 2026-07-05): full≡incremental CONTAINS parity. The full index emits parent→child
// CONTAINS edges; the `-file` reindex DELETED a file's CONTAINS (DeleteFileEdgesAndNodesTx)
// but never re-emitted them, so the containment graph THINNED to ZERO for that file on every
// L6 edit. This end-to-end red→green full-indexes a class file (Widget contains draw/resize),
// asserts CONTAINS exist, then `-file` reindexes it (hash changed so no short-circuit) and
// asserts the CONTAINS count is PRESERVED (not dropped to 0).
//
// RED before the fix: containsCount after the reindex == 0.
func TestIncrementalReindexPreservesContainsEdges(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(filepath.Join(repo, "pkg"), 0o755); err != nil {
		t.Fatal(err)
	}
	widgetRel := "pkg/widget.py"
	if err := os.WriteFile(filepath.Join(repo, widgetRel),
		[]byte("class Widget:\n    def draw(self):\n        return 1\n\n    def resize(self):\n        return 2\n"), 0o644); err != nil {
		t.Fatal(err)
	}

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
	full := exec.Command(bin, "-root", repo, "-output", dbPath)
	if out, err := full.CombinedOutput(); err != nil {
		t.Fatalf("full index: %v\n%s", err, out)
	}
	fullContains := containsEdgeCount(t, dbPath, "%widget.py")
	if fullContains < 2 {
		t.Fatalf("full index produced %d CONTAINS for widget.py; want >=2 (Widget->draw, Widget->resize) — fixture invalid", fullContains)
	}

	// Change the file (add a method) so the SHA-256 short-circuit does NOT fire and the
	// incremental resolver actually re-runs the delete+reinsert for this file.
	if err := os.WriteFile(filepath.Join(repo, widgetRel),
		[]byte("class Widget:\n    def draw(self):\n        return 1\n\n    def resize(self):\n        return 2\n\n    def extra(self):\n        return 3\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	incr := exec.Command(bin, "-root", repo, "-output", dbPath, "-file", widgetRel)
	if out, err := incr.CombinedOutput(); err != nil {
		t.Fatalf("incremental reindex: %v\n%s", err, out)
	}

	incrContains := containsEdgeCount(t, dbPath, "%widget.py")
	if incrContains == 0 {
		t.Fatalf("after -file reindex widget.py has 0 CONTAINS edges — the containment graph was DELETED and never re-emitted (the B1 regression)")
	}
	if incrContains < fullContains {
		t.Errorf("after -file reindex CONTAINS dropped %d -> %d for widget.py (added a method, so it should be >= the full count)", fullContains, incrContains)
	}
}

// containsEdgeCount returns the number of CONTAINS edges whose source_file matches the
// given LIKE pattern.
func containsEdgeCount(t *testing.T, dbPath, fileLike string) int {
	t.Helper()
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var n int
	if err := db.QueryRow(
		`SELECT COUNT(*) FROM edges WHERE type = 'CONTAINS' AND source_file LIKE ?`, fileLike,
	).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}
