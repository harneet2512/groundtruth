package main

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
)

// ─────────────────────────────────────────────────────────────────────────────
// Executor-contract tests for `gt-index -root R -output G.db -file F`:
//   (a) failure classes exit NONZERO with a clear one-line stderr; success (incl.
//       honest short-circuit) exits 0;
//   (b) stdout carries a one-line machine-readable summary with the required
//       fields {changed, edges_replaced, incoming_restored, short_circuited,
//       post_revision} so the Python overlay parses results without log-scraping;
//   (c) post_revision is a deterministic content hash — identical runs match, a
//       real change differs.
// ─────────────────────────────────────────────────────────────────────────────

var (
	sharedBinOnce sync.Once
	sharedBinPath string
	sharedBinErr  error
)

// buildGTIndex builds the gt-index binary ONCE per test process (tree-sitter
// builds are slow) and returns its path.
func buildGTIndex(t *testing.T) string {
	t.Helper()
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	sharedBinOnce.Do(func() {
		dir, err := os.MkdirTemp("", "gtindex-bin")
		if err != nil {
			sharedBinErr = err
			return
		}
		bin := filepath.Join(dir, "gt-index")
		if runtime.GOOS == "windows" {
			bin += ".exe"
		}
		build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-o", bin, ".")
		build.Env = append(os.Environ(), "CGO_ENABLED=1")
		if out, berr := build.CombinedOutput(); berr != nil {
			sharedBinErr = &execErr{msg: string(out), err: berr}
			return
		}
		sharedBinPath = bin
	})
	if sharedBinErr != nil {
		t.Fatalf("build gt-index: %v", sharedBinErr)
	}
	return sharedBinPath
}

type execErr struct {
	msg string
	err error
}

func (e *execErr) Error() string { return e.err.Error() + "\n" + e.msg }

// runFile runs `gt-index -root repo -output db -file rel`, returning stdout,
// stderr and the process exit code.
func runFile(t *testing.T, bin, repo, db, rel string) (stdout, stderr string, code int) {
	t.Helper()
	cmd := exec.Command(bin, "-root", repo, "-output", db, "-file", rel)
	var o, e bytes.Buffer
	cmd.Stdout = &o
	cmd.Stderr = &e
	err := cmd.Run()
	code = 0
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			code = ee.ExitCode()
		} else {
			t.Fatalf("run -file %s: unexpected error: %v", rel, err)
		}
	}
	return o.String(), e.String(), code
}

// fullIndex runs a full index of repo into db.
func fullIndex(t *testing.T, bin, repo, db string) {
	t.Helper()
	cmd := exec.Command(bin, "-root", repo, "-output", db)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("full index: %v\n%s", err, out)
	}
}

// lastJSONLine returns the last non-empty line of stdout parsed as a JSON object
// map (for presence checks).
func lastJSONLine(t *testing.T, stdout string) (map[string]json.RawMessage, string) {
	t.Helper()
	var line string
	for _, l := range strings.Split(strings.TrimSpace(stdout), "\n") {
		if s := strings.TrimSpace(l); s != "" {
			line = s
		}
	}
	if line == "" {
		t.Fatalf("no stdout summary line; stdout=%q", stdout)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal([]byte(line), &m); err != nil {
		t.Fatalf("summary line is not valid JSON: %v\nline=%s", err, line)
	}
	return m, line
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

const pyA = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
const pyB = "def foo():\n    return 1\n\ndef bar():\n    return foo()\n" // bar now CALLS foo (edge change)
const pyC = "def foo():\n    return 1\n\ndef bar():\n    return foo()\n\ndef baz():\n    return bar()\n"

// TestIncrementalSummarySchema — the -file summary MUST carry all required
// fields. This is also the M2-guard: dropping `short_circuited` from the emitted
// JSON makes the presence assertion below bite.
func TestIncrementalSummarySchema(t *testing.T) {
	bin := buildGTIndex(t)
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	db := filepath.Join(tmp, "graph.db")
	rel := "pkg/mod.py"
	writeFile(t, filepath.Join(repo, rel), pyA)
	fullIndex(t, bin, repo, db)

	// Change the file so the reindex actually runs (no short-circuit).
	writeFile(t, filepath.Join(repo, rel), pyB)
	stdout, stderr, code := runFile(t, bin, repo, db, rel)
	if code != 0 {
		t.Fatalf("reindex exit=%d, want 0; stderr=%s", code, stderr)
	}
	m, line := lastJSONLine(t, stdout)

	required := []string{"changed", "edges_replaced", "incoming_restored", "short_circuited", "post_revision"}
	for _, k := range required {
		if _, ok := m[k]; !ok {
			t.Errorf("summary missing required field %q\nline=%s", k, line)
		}
	}

	var s struct {
		Changed        bool   `json:"changed"`
		ShortCircuited bool   `json:"short_circuited"`
		PostRevision   string `json:"post_revision"`
	}
	if err := json.Unmarshal([]byte(line), &s); err != nil {
		t.Fatal(err)
	}
	if s.ShortCircuited {
		t.Errorf("short_circuited=true after a real content change")
	}
	if !s.Changed {
		t.Errorf("changed=false after a real content change (edges added)")
	}
	if len(s.PostRevision) != 64 {
		t.Errorf("post_revision is not a hex sha256 (len=%d): %q", len(s.PostRevision), s.PostRevision)
	}
}

// TestIncrementalShortCircuit — an unchanged -file reindex short-circuits: exit 0,
// short_circuited=true, changed=false, and post_revision EQUALS the prior run's
// (the db was untouched).
func TestIncrementalShortCircuit(t *testing.T) {
	bin := buildGTIndex(t)
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	db := filepath.Join(tmp, "graph.db")
	rel := "pkg/mod.py"
	writeFile(t, filepath.Join(repo, rel), pyA)
	fullIndex(t, bin, repo, db)

	// First reindex (content changed A->B) establishes a revision.
	writeFile(t, filepath.Join(repo, rel), pyB)
	out1, _, code1 := runFile(t, bin, repo, db, rel)
	if code1 != 0 {
		t.Fatalf("first reindex exit=%d", code1)
	}
	_, line1 := lastJSONLine(t, out1)
	var s1 struct {
		PostRevision string `json:"post_revision"`
	}
	json.Unmarshal([]byte(line1), &s1)

	// Second reindex WITHOUT changing the file → short-circuit.
	out2, stderr2, code2 := runFile(t, bin, repo, db, rel)
	if code2 != 0 {
		t.Fatalf("short-circuit reindex exit=%d, want 0; stderr=%s", code2, stderr2)
	}
	_, line2 := lastJSONLine(t, out2)
	var s2 struct {
		Changed        bool   `json:"changed"`
		ShortCircuited bool   `json:"short_circuited"`
		PostRevision   string `json:"post_revision"`
	}
	if err := json.Unmarshal([]byte(line2), &s2); err != nil {
		t.Fatal(err)
	}
	if !s2.ShortCircuited {
		t.Errorf("second identical reindex did not short-circuit: %s", line2)
	}
	if s2.Changed {
		t.Errorf("changed=true on a short-circuit (nothing was reindexed): %s", line2)
	}
	if s2.PostRevision != s1.PostRevision {
		t.Errorf("post_revision changed across a no-op short-circuit: %q != %q", s2.PostRevision, s1.PostRevision)
	}
	if len(s2.PostRevision) != 64 {
		t.Errorf("short-circuit post_revision not a hex sha256: %q", s2.PostRevision)
	}
}

// TestIncrementalDeterminismDetectsChange — the end-to-end revision invariants:
// a real content change (adding a function + call = new nodes/edges) MUST move
// post_revision; a no-op re-run must NOT. (Store-level M1 mutation proves edges
// are the load-bearing input.)
func TestIncrementalDeterminismDetectsChange(t *testing.T) {
	bin := buildGTIndex(t)
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	db := filepath.Join(tmp, "graph.db")
	rel := "pkg/mod.py"
	writeFile(t, filepath.Join(repo, rel), pyB)
	fullIndex(t, bin, repo, db)

	// Baseline reindex (already B → but hash differs from full's stored? full stored B's hash;
	// force a change to C to get a reindex).
	writeFile(t, filepath.Join(repo, rel), pyC)
	outC, _, _ := runFile(t, bin, repo, db, rel)
	_, lineC := lastJSONLine(t, outC)
	var sC struct {
		Changed      bool   `json:"changed"`
		PostRevision string `json:"post_revision"`
	}
	json.Unmarshal([]byte(lineC), &sC)
	if !sC.Changed {
		t.Errorf("changed=false after adding a function+call")
	}

	// Revert to B → different graph again → revision must differ from C's.
	writeFile(t, filepath.Join(repo, rel), pyB)
	outB, _, _ := runFile(t, bin, repo, db, rel)
	_, lineB := lastJSONLine(t, outB)
	var sB struct {
		PostRevision string `json:"post_revision"`
	}
	json.Unmarshal([]byte(lineB), &sB)

	if sB.PostRevision == sC.PostRevision {
		t.Errorf("distinct graphs (B vs C) produced the same post_revision: %q", sB.PostRevision)
	}
}

// TestIncrementalFailMissingDB — reindex against a nonexistent -output db exits
// nonzero with a clear one-line stderr and NO stdout summary.
func TestIncrementalFailMissingDB(t *testing.T) {
	bin := buildGTIndex(t)
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	rel := "pkg/mod.py"
	writeFile(t, filepath.Join(repo, rel), pyA)
	db := filepath.Join(tmp, "does-not-exist.db")

	stdout, stderr, code := runFile(t, bin, repo, db, rel)
	if code == 0 {
		t.Fatalf("missing-db reindex exited 0; want nonzero. stdout=%q", stdout)
	}
	assertOneLineStderr(t, stderr)
	if strings.TrimSpace(stdout) != "" {
		t.Errorf("failure path printed a stdout summary: %q", stdout)
	}
}

// TestIncrementalFailMissingFile — reindex of a file that does not exist on disk.
func TestIncrementalFailMissingFile(t *testing.T) {
	bin := buildGTIndex(t)
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	db := filepath.Join(tmp, "graph.db")
	writeFile(t, filepath.Join(repo, "pkg/mod.py"), pyA)
	fullIndex(t, bin, repo, db)

	stdout, stderr, code := runFile(t, bin, repo, db, "pkg/gone.py")
	if code == 0 {
		t.Fatalf("missing-file reindex exited 0; want nonzero. stdout=%q", stdout)
	}
	assertOneLineStderr(t, stderr)
}

// TestIncrementalFailUnsupportedExt — a file whose extension has no language spec
// is a clean parse-class failure (nonzero, clear stderr), not a silent no-op.
func TestIncrementalFailUnsupportedExt(t *testing.T) {
	bin := buildGTIndex(t)
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	db := filepath.Join(tmp, "graph.db")
	writeFile(t, filepath.Join(repo, "pkg/mod.py"), pyA)
	fullIndex(t, bin, repo, db)
	writeFile(t, filepath.Join(repo, "pkg/data.zzz"), "not source\n")

	stdout, stderr, code := runFile(t, bin, repo, db, "pkg/data.zzz")
	if code == 0 {
		t.Fatalf("unsupported-ext reindex exited 0; want nonzero. stdout=%q", stdout)
	}
	assertOneLineStderr(t, stderr)
}

// TestIncrementalFailUnusableDB — the "unwritable db" failure class, portable
// form: -output points at a path that exists but cannot be opened as a SQLite
// db (a DIRECTORY). Must exit nonzero with a clear one-line stderr, never hang
// or print a success summary.
func TestIncrementalFailUnusableDB(t *testing.T) {
	bin := buildGTIndex(t)
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	rel := "pkg/mod.py"
	writeFile(t, filepath.Join(repo, rel), pyA)
	dbDir := filepath.Join(tmp, "db-as-dir")
	if err := os.MkdirAll(dbDir, 0o755); err != nil {
		t.Fatal(err)
	}

	stdout, stderr, code := runFile(t, bin, repo, dbDir, rel)
	if code == 0 {
		t.Fatalf("directory-as-db reindex exited 0; want nonzero. stdout=%q", stdout)
	}
	assertOneLineStderr(t, stderr)
	if strings.TrimSpace(stdout) != "" {
		t.Errorf("failure path printed a stdout summary: %q", stdout)
	}
}

// assertOneLineStderr checks the stderr carries exactly ONE non-empty line (the
// clear failure message the executor contract promises).
func assertOneLineStderr(t *testing.T, stderr string) {
	t.Helper()
	trimmed := strings.TrimRight(stderr, "\n")
	if trimmed == "" {
		t.Fatalf("failure produced empty stderr")
	}
	if strings.Contains(trimmed, "\n") {
		t.Errorf("failure stderr is not one line:\n%s", stderr)
	}
}
