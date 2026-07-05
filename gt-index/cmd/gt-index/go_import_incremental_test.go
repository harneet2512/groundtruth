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

// B2 (Fable 2026-07-05): full≡incremental parity for Go/Rust fileMap registration. The full
// index registers Go module/package/vendor + Rust crate fileMap aliases; the `-file` reindex
// registered ONLY TS/JS — a LANGUAGE-KEYED inequivalence. B2 mirrors those 4 registrations
// onto the incremental path (purely additive fileMap aliases; identical to the full path).
//
// This is a full≡incremental PARITY GUARD, not an isolated RED-proof of B2: a simple
// intra-module Go import (`a.Helper()`, package name == dir) resolves via buildImportIndex's
// dir-suffix fallback on BOTH paths regardless of the RegisterGo* calls, so reverting B2
// alone does not flip THIS edge. The test guards that the incremental Go path keeps resolving
// the cross-package call via the import (not name_match) across a `-file` reindex — a
// regression guard for the whole incremental Go pipeline. B2's own correctness rests on being
// byte-identical registration to the full index (build-clean + full-suite no-regression).
func TestIncrementalReindexPreservesGoModuleImport(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	for _, d := range []string{"pkg/a", "pkg/b"} {
		if err := os.MkdirAll(filepath.Join(repo, d), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(repo, "go.mod"),
		[]byte("module example.com/proj\n\ngo 1.22\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "pkg/a/a.go"),
		[]byte("package a\n\nfunc Helper() int {\n\treturn 1\n}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	bRel := "pkg/b/b.go"
	if err := os.WriteFile(filepath.Join(repo, bRel),
		[]byte("package b\n\nimport \"example.com/proj/pkg/a\"\n\nfunc Use() int {\n\treturn a.Helper()\n}\n"), 0o644); err != nil {
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
	if m := goImportEdgeMethod(t, dbPath); m == "" {
		t.Fatalf("full index produced no b.Use -> a.Helper edge (fixture invalid)")
	} else if m == "name_match" {
		t.Fatalf("full index resolved a.Helper() as name_match; expected import (fixture/resolver mismatch)")
	}

	// Change b.go (hash differs → no short-circuit) but keep the cross-package call.
	if err := os.WriteFile(filepath.Join(repo, bRel),
		[]byte("package b\n\nimport \"example.com/proj/pkg/a\"\n\nfunc Use() int {\n\treturn a.Helper()\n}\n\nfunc Also() int {\n\treturn 0\n}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	incr := exec.Command(bin, "-root", repo, "-output", dbPath, "-file", bRel)
	if out, err := incr.CombinedOutput(); err != nil {
		t.Fatalf("incremental reindex: %v\n%s", err, out)
	}

	m := goImportEdgeMethod(t, dbPath)
	if m == "" {
		t.Fatalf("after -file reindex the b.Use -> a.Helper edge is GONE (Go module paths absent on the incremental path — the B2 regression)")
	}
	if m == "name_match" {
		t.Fatalf("after -file reindex a.Helper() degraded to name_match (Go module fileMap registration missing on -file — the B2 regression)")
	}
}

// goImportEdgeMethod returns the resolution_method of the CALLS edge from a node in b.go to
// Helper in a.go, or "" if no such edge exists.
func goImportEdgeMethod(t *testing.T, dbPath string) string {
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
		    AND tgt.name = 'Helper'
		    AND tgt.file_path LIKE '%a.go'
		    AND src.file_path LIKE '%b.go'
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
