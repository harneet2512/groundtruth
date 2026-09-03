package main

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sync"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// Publication is one transaction today: the core graph and the resolution
// analysis commit or die together, so a single candidate the store refuses to
// derive discards every file, symbol and structural edge the producer already
// paid to extract. That is what aborted the paid gate run. These tests pin the
// two-phase contract from the outside, through the real binary: a phase-2
// failure must leave a readable core graph carrying a receipt that says the
// analysis is absent and why.

func twoPhaseFixtureRepo(t *testing.T) string {
	t.Helper()
	repo := filepath.Join(t.TempDir(), "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "mod.py"), []byte("def target(value):\n    return value + 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "caller.py"), []byte("from mod import target\n\ndef local_target(value):\n    return value\n\ndef caller(value):\n    local_target(value)\n    return target(value)\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return repo
}

// The cgo SQLite build dominates these tests, so the binary is built once for
// the whole file rather than once per test.
var (
	twoPhaseBinOnce sync.Once
	twoPhaseBinPath string
	twoPhaseBinDir  string
	twoPhaseBinErr  error
)

func TestMain(m *testing.M) {
	code := m.Run()
	if twoPhaseBinDir != "" {
		_ = os.RemoveAll(twoPhaseBinDir)
	}
	os.Exit(code)
}

func buildTwoPhaseIndexBinary(t *testing.T) string {
	t.Helper()
	twoPhaseBinOnce.Do(func() {
		dir, err := os.MkdirTemp("", "gt-index-two-phase-")
		if err != nil {
			twoPhaseBinErr = err
			return
		}
		twoPhaseBinDir = dir
		bin := filepath.Join(dir, "gt-index")
		if runtime.GOOS == "windows" {
			bin += ".exe"
		}
		build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-ldflags", testBuildLDFlags, "-o", bin, ".")
		build.Env = append(os.Environ(), "CGO_ENABLED=1")
		if out, buildErr := build.CombinedOutput(); buildErr != nil {
			twoPhaseBinErr = fmt.Errorf("build gt-index: %v\n%s", buildErr, out)
			return
		}
		twoPhaseBinPath = bin
	})
	if twoPhaseBinErr != nil {
		t.Fatalf("%v", twoPhaseBinErr)
	}
	return twoPhaseBinPath
}

// twoPhaseMeta returns the project_meta value for key, or "" when the row is
// absent. Absence is the interesting state here, so it must not be an error.
func twoPhaseMeta(t *testing.T, db *sql.DB, key string) string {
	t.Helper()
	var value string
	err := db.QueryRow(`SELECT value FROM project_meta WHERE key=?`, key).Scan(&value)
	if err == sql.ErrNoRows {
		return ""
	}
	if err != nil {
		t.Fatalf("read project_meta key: %v", err)
	}
	return value
}

func twoPhaseCount(t *testing.T, db *sql.DB, query string) int {
	t.Helper()
	var n int
	if err := db.QueryRow(query).Scan(&n); err != nil {
		t.Fatalf("count query failed: %v", err)
	}
	return n
}

func TestAnalysisFailureLeavesCoreGraphWithNamedAnalysisState(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	repo := twoPhaseFixtureRepo(t)
	bin := buildTwoPhaseIndexBinary(t)
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-workers", "2")
	index.Env = append(os.Environ(), "GT_INJECT_ANALYSIS_FAILURE=1")
	if err := index.Run(); err != nil {
		t.Fatalf("an analysis failure must not fail the build when analysis is not required: %v", err)
	}
	if _, err := os.Stat(dbPath); err != nil {
		t.Fatal("analysis failure discarded the core graph")
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	if state := twoPhaseMeta(t, db, "analysis_state"); state != "failed" {
		t.Fatalf("analysis_state after an injected phase-2 failure = %q, want \"failed\"", state)
	}
	if reason := twoPhaseMeta(t, db, "analysis_failure_reason"); reason == "" {
		t.Fatal("analysis failure recorded no reason")
	}
	if state := twoPhaseMeta(t, db, "core_phase_state"); state != "committed" {
		t.Fatalf("core_phase_state = %q, want \"committed\"", state)
	}
	if nodes := twoPhaseCount(t, db, `SELECT count(*) FROM nodes WHERE label != 'Callsite'`); nodes == 0 {
		t.Fatal("core graph has no symbols after an analysis failure")
	}
	if calls := twoPhaseCount(t, db, `SELECT count(*) FROM edges WHERE type='CALLS'`); calls == 0 {
		t.Fatal("core graph has no structural CALLS edges after an analysis failure")
	}

	// Phase 2 rolls back as a unit: no half-written analysis may survive.
	if n := twoPhaseCount(t, db, `SELECT count(*) FROM resolution_callsites`); n != 0 {
		t.Fatalf("rolled-back analysis left %d resolution_callsites rows", n)
	}
	if n := twoPhaseCount(t, db, `SELECT count(*) FROM resolution_candidates`); n != 0 {
		t.Fatalf("rolled-back analysis left %d resolution_candidates rows", n)
	}
	if n := twoPhaseCount(t, db, `SELECT count(*) FROM nodes WHERE label='Callsite'`); n != 0 {
		t.Fatalf("rolled-back analysis left %d Callsite nodes", n)
	}
	if complete := twoPhaseMeta(t, db, "graph_resolution_complete"); complete != "" {
		t.Fatalf("rolled-back analysis still claims graph_resolution_complete=%q", complete)
	}
	if receipt := twoPhaseMeta(t, db, "graph_completion_receipt"); receipt != "" {
		t.Fatal("rolled-back analysis still published a graph completion receipt")
	}
}

func TestAnalysisFailureIsStillFatalWhenOperatorRequiresAnalysis(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	repo := twoPhaseFixtureRepo(t)
	bin := buildTwoPhaseIndexBinary(t)
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-workers", "2")
	index.Env = append(os.Environ(), "GT_INJECT_ANALYSIS_FAILURE=1", "GT_REQUIRE_ANALYSIS=1")
	if err := index.Run(); err == nil {
		t.Fatal("gt-index exited 0 with GT_REQUIRE_ANALYSIS=1 and an injected analysis failure")
	}
	// Fail-closed is an exit code, not a lost graph: the operator still gets the
	// core graph and its receipt to diagnose from.
	if _, err := os.Stat(dbPath); err != nil {
		t.Fatal("required-analysis failure discarded the core graph instead of only failing the exit code")
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if state := twoPhaseMeta(t, db, "analysis_state"); state != "failed" {
		t.Fatalf("analysis_state under GT_REQUIRE_ANALYSIS=1 = %q, want \"failed\"", state)
	}
	if state := twoPhaseMeta(t, db, "core_phase_state"); state != "committed" {
		t.Fatalf("core_phase_state under GT_REQUIRE_ANALYSIS=1 = %q, want \"committed\"", state)
	}
}

// An analysis that cannot finish inside the run budget is the case that
// motivates the split as much as a rejected candidate does: boa's analysis
// phase still exceeds its budget, so today it publishes nothing at all. Skipping
// the phase must produce a core graph whose receipt says the analysis was never
// run -- a different fact from an analysis that ran and found nothing.
func TestSkippedAnalysisPublishesACoreGraphMarkedNotRun(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	repo := twoPhaseFixtureRepo(t)
	bin := buildTwoPhaseIndexBinary(t)
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-workers", "2")
	index.Env = append(os.Environ(), "GT_SKIP_ANALYSIS=1")
	if err := index.Run(); err != nil {
		t.Fatalf("skipping the analysis phase must still publish a core graph: %v", err)
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if state := twoPhaseMeta(t, db, "analysis_state"); state != "not_run" {
		t.Fatalf("analysis_state with GT_SKIP_ANALYSIS=1 = %q, want \"not_run\"", state)
	}
	if reason := twoPhaseMeta(t, db, "analysis_failure_reason"); reason == "" {
		t.Fatal("a skipped analysis recorded no reason")
	}
	if state := twoPhaseMeta(t, db, "core_phase_state"); state != "committed" {
		t.Fatalf("core_phase_state with GT_SKIP_ANALYSIS=1 = %q, want \"committed\"", state)
	}
	if nodes := twoPhaseCount(t, db, `SELECT count(*) FROM nodes`); nodes == 0 {
		t.Fatal("skipping the analysis phase produced an empty graph")
	}
	if calls := twoPhaseCount(t, db, `SELECT count(*) FROM edges WHERE type='CALLS'`); calls == 0 {
		t.Fatal("skipping the analysis phase produced no structural CALLS edges")
	}
	if n := twoPhaseCount(t, db, `SELECT count(*) FROM resolution_callsites`); n != 0 {
		t.Fatalf("an unrun analysis published %d resolution_callsites rows", n)
	}
}

// A malformed operator setting fails closed before any work is skipped.
func TestMalformedAnalysisDeadlineFailsClosed(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	repo := twoPhaseFixtureRepo(t)
	bin := buildTwoPhaseIndexBinary(t)
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-workers", "2")
	index.Env = append(os.Environ(), "GT_ANALYSIS_DEADLINE_SECONDS=not-a-number")
	if err := index.Run(); err == nil {
		t.Fatal("a malformed GT_ANALYSIS_DEADLINE_SECONDS was accepted")
	}
}

func TestCompleteBuildPublishesBothPhaseReceipts(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	repo := twoPhaseFixtureRepo(t)
	bin := buildTwoPhaseIndexBinary(t)
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-workers", "2")
	if out, err := index.CombinedOutput(); err != nil {
		t.Fatalf("full index: %v\n%s", err, out)
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	if state := twoPhaseMeta(t, db, "core_phase_state"); state != "committed" {
		t.Fatalf("core_phase_state = %q, want \"committed\"", state)
	}
	if state := twoPhaseMeta(t, db, "analysis_state"); state != "complete" {
		t.Fatalf("analysis_state = %q, want \"complete\"", state)
	}
	if reason := twoPhaseMeta(t, db, "analysis_failure_reason"); reason != "" {
		t.Fatalf("complete analysis recorded a failure reason %q", reason)
	}
	// Each phase carries its own hash-bound receipt, so a reader can tell which
	// layer it got without re-running the producer.
	corePayload := twoPhaseMeta(t, db, "core_phase_receipt")
	if corePayload == "" {
		t.Fatal("core_phase_receipt is absent from a complete build")
	}
	coreSum := sha256.Sum256([]byte(corePayload))
	if got := twoPhaseMeta(t, db, "core_phase_receipt_sha256"); got != hex.EncodeToString(coreSum[:]) {
		t.Fatal("core_phase_receipt_sha256 does not bind core_phase_receipt")
	}
	analysisPayload := twoPhaseMeta(t, db, "analysis_phase_receipt")
	if analysisPayload == "" {
		t.Fatal("analysis_phase_receipt is absent from a complete build")
	}
	analysisSum := sha256.Sum256([]byte(analysisPayload))
	if got := twoPhaseMeta(t, db, "analysis_phase_receipt_sha256"); got != hex.EncodeToString(analysisSum[:]) {
		t.Fatal("analysis_phase_receipt_sha256 does not bind analysis_phase_receipt")
	}
	// "analysis ran and found nothing" must be distinguishable from "analysis
	// not run", so the receipt has to carry the counts, not just the state.
	if calls := twoPhaseCount(t, db, `SELECT count(*) FROM resolution_callsites`); calls == 0 {
		t.Fatal("complete analysis published no callsites")
	}
}
