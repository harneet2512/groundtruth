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

// TestResolutionContractIsWrittenByTheRealCLI exercises the producer boundary,
// rather than only testing the resolver/store helpers in isolation. The
// artifact must contain revision-bound callsites and candidates whose target
// identities are foreign-keyed to producer-emitted symbols.
func TestResolutionContractIsWrittenByTheRealCLI(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "caller.py"), []byte("def target(value):\n    return value + 1\n\ndef caller(value):\n    return target(value)\n"), 0o644); err != nil {
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
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-workers", "2")
	if out, err := index.CombinedOutput(); err != nil {
		t.Fatalf("full index: %v\n%s", err, out)
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var complete, failures string
	if err := db.QueryRow("SELECT value FROM project_meta WHERE key='resolution_complete'").Scan(&complete); err != nil {
		t.Fatalf("resolution_complete metadata: %v", err)
	}
	if complete != "1" {
		t.Fatalf("resolution_complete = %q, want 1", complete)
	}
	if err := db.QueryRow("SELECT value FROM project_meta WHERE key='parse_failures'").Scan(&failures); err != nil {
		t.Fatalf("parse_failures metadata: %v", err)
	}
	if failures != "0" {
		t.Fatalf("parse_failures = %q, want 0", failures)
	}
	var calls, candidates, symbols int
	if err := db.QueryRow("SELECT count(*) FROM resolution_callsites").Scan(&calls); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM resolution_candidates").Scan(&candidates); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM resolution_symbols").Scan(&symbols); err != nil {
		t.Fatal(err)
	}
	if calls == 0 || candidates == 0 || symbols == 0 {
		t.Fatalf("producer sidecar is empty: callsites=%d candidates=%d symbols=%d", calls, candidates, symbols)
	}
	var orphan int
	if err := db.QueryRow(`SELECT count(*) FROM resolution_candidates c
		LEFT JOIN resolution_symbols s ON s.stable_id=c.target_stable_id
		WHERE s.stable_id IS NULL`).Scan(&orphan); err != nil {
		t.Fatal(err)
	}
	if orphan != 0 {
		t.Fatalf("candidate rows with no producer symbol identity: %d", orphan)
	}
	var mismatch int
	if err := db.QueryRow(`SELECT count(*) FROM resolution_callsites c
		WHERE c.candidate_count != (SELECT count(*) FROM resolution_candidates k WHERE k.callsite_id=c.callsite_id)`).Scan(&mismatch); err != nil {
		t.Fatal(err)
	}
	if mismatch != 0 {
		t.Fatalf("callsite candidate counts disagree with retained rows: %d", mismatch)
	}
}
