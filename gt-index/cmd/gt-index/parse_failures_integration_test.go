package main

import (
	"database/sql"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
	_ "github.com/mattn/go-sqlite3"
)

func TestCollectParseResultsCountsAndIdentifiesFailures(t *testing.T) {
	files := []walker.SourceFile{{Path: "valid.py"}, {Path: "broken.py"}}
	resultCh := make(chan fileParseResult, len(files))
	resultCh <- fileParseResult{fileIdx: 0, result: &parser.ParseResult{}}
	resultCh <- fileParseResult{fileIdx: 1, err: errors.New("deterministic parse failure")}
	close(resultCh)

	results, failures, samples := collectParseResults(files, resultCh)
	if results[0] == nil || results[1] != nil {
		t.Fatalf("unexpected result slots: %#v", results)
	}
	if failures != 1 {
		t.Fatalf("parse failures = %d; want 1", failures)
	}
	if len(samples) != 1 || samples[0] != "broken.py: deterministic parse failure" {
		t.Fatalf("failure samples = %#v; want broken.py diagnostic", samples)
	}
}

func TestFullIndexPersistsZeroParseFailures(t *testing.T) {
	if testing.Short() {
		t.Skip("builds and executes the gt-index binary; skipped under -short")
	}

	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(
		filepath.Join(repo, "valid.py"),
		[]byte("def answer():\n    return 42\n"),
		0o644,
	); err != nil {
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
	index := exec.Command(bin, "-root", repo, "-output", dbPath)
	if out, err := index.CombinedOutput(); err != nil {
		t.Fatalf("full index: %v\n%s", err, out)
	}

	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	var got string
	if err := db.QueryRow(
		`SELECT value FROM project_meta WHERE key = 'parse_failures'`,
	).Scan(&got); err != nil {
		t.Fatalf("read project_meta.parse_failures: %v", err)
	}
	if got != "0" {
		t.Fatalf("project_meta.parse_failures = %q; want %q", got, "0")
	}
}

func TestFullIndexRetainsAmbiguousResolutionCandidates(t *testing.T) {
	if testing.Short() {
		t.Skip("builds and executes the gt-index binary; skipped under -short")
	}
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	fixtures := map[string]string{
		"one.py":    "def run():\n    return 1\n",
		"two.py":    "def run():\n    return 2\n",
		"caller.py": "def call():\n    return run()\n",
	}
	for name, content := range fixtures {
		if err := os.WriteFile(filepath.Join(repo, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
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
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-closure=false")
	if out, err := index.CombinedOutput(); err != nil {
		t.Fatalf("full index: %v\n%s", err, out)
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var candidates, selected int
	if err := db.QueryRow(`SELECT COUNT(*), COALESCE(SUM(rc.selected), 0)
		FROM resolution_candidates rc
		JOIN resolution_callsites cs ON cs.callsite_id = rc.callsite_id
		WHERE cs.source_file = 'caller.py' AND cs.source_line = 2`).Scan(&candidates, &selected); err != nil {
		t.Fatalf("read resolution candidate sidecar: %v", err)
	}
	if candidates != 2 || selected != 0 {
		t.Fatalf("candidate sidecar rows = %d (selected %d); want 2 candidates and no selected target", candidates, selected)
	}
}

func TestFullIndexPersistsNonzeroParseFailuresWhenUnreadableFilesAreSupported(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows does not enforce Unix unreadable-file mode bits")
	}
	if testing.Short() {
		t.Skip("builds and executes the gt-index binary; skipped under -short")
	}

	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "valid.py"), []byte("def valid():\n    return 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	unreadable := filepath.Join(repo, "unreadable.py")
	if err := os.WriteFile(unreadable, []byte("def hidden():\n    return 2\n"), 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(unreadable, 0o644) })
	if _, err := os.ReadFile(unreadable); err == nil {
		t.Skip("filesystem does not enforce unreadable file permissions")
	}

	bin := filepath.Join(tmp, "gt-index")
	build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-o", bin, ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=1")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build gt-index: %v\n%s", err, out)
	}

	dbPath := filepath.Join(tmp, "graph.db")
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-closure=false")
	if out, err := index.CombinedOutput(); err != nil {
		t.Fatalf("full index: %v\n%s", err, out)
	}

	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var got string
	if err := db.QueryRow(`SELECT value FROM project_meta WHERE key = 'parse_failures'`).Scan(&got); err != nil {
		t.Fatal(err)
	}
	if got != "1" {
		t.Fatalf("project_meta.parse_failures = %q; want %q", got, "1")
	}
}

func TestFullIndexPersistsConservedResolutionProvenance(t *testing.T) {
	if testing.Short() {
		t.Skip("builds and executes the gt-index binary")
	}
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	code := "def target():\n    return 1\n\ndef target():\n    return 2\n\ndef caller():\n    target()\n    missing()\n"
	if err := os.WriteFile(filepath.Join(repo, "calls.py"), []byte(code), 0o644); err != nil {
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
	if out, err := exec.Command(bin, "-root", repo, "-output", dbPath, "-closure=false").CombinedOutput(); err != nil {
		t.Fatalf("full index: %v\n%s", err, out)
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var state string
	var count int
	var selected sql.NullString
	if err := db.QueryRow(`SELECT dispatch_state,candidate_count,selected_target_stable_id FROM resolution_callsites WHERE callee='target'`).Scan(&state, &count, &selected); err != nil {
		t.Fatal(err)
	}
	if state != "ambiguous" || count != 2 || selected.Valid {
		t.Fatalf("ambiguous callsite = state=%q count=%d selected=%v", state, count, selected)
	}
	var rows, minOrdinal, maxOrdinal, selectedCount int
	if err := db.QueryRow(`SELECT COUNT(*),MIN(ordinal),MAX(ordinal),SUM(selected) FROM resolution_candidates c JOIN resolution_callsites s USING(callsite_id) WHERE s.callee='target'`).Scan(&rows, &minOrdinal, &maxOrdinal, &selectedCount); err != nil {
		t.Fatal(err)
	}
	if rows != 2 || minOrdinal != 0 || maxOrdinal != 1 || selectedCount != 0 {
		t.Fatalf("candidate conservation = rows=%d ordinals=%d..%d selected=%d", rows, minOrdinal, maxOrdinal, selectedCount)
	}
	if err := db.QueryRow(`SELECT dispatch_state,candidate_count FROM resolution_callsites WHERE callee='missing'`).Scan(&state, &count); err != nil {
		t.Fatal(err)
	}
	if state != "zero" || count != 0 {
		t.Fatalf("zero callsite = state=%q count=%d", state, count)
	}
}
