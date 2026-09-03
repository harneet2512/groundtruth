package main

import (
	"database/sql"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sync"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// The derived analysis layers -- co-change, communities, processes -- exist as
// packages under internal/ and have no call site in the publication path, so
// their tables are absent or empty in every graph the producer publishes.
//
// These tests pin the wiring at the producer boundary, through the real CLI,
// because that is the only place the absence is observable: every package is
// green in isolation and the published graph is still empty.

var (
	derivedBinaryOnce sync.Once
	derivedBinaryPath string
	derivedBinaryErr  error
)

// buildDerivedIndexer builds the indexer once per test binary. Two tests here
// index repositories with it, and a rebuild per test would multiply the
// runtime of a fixture a pre-push hook re-executes.
func buildDerivedIndexer(t *testing.T) string {
	t.Helper()
	derivedBinaryOnce.Do(func() {
		dir, err := os.MkdirTemp("", "gt-derived-bin")
		if err != nil {
			derivedBinaryErr = err
			return
		}
		bin := filepath.Join(dir, "gt-index")
		if runtime.GOOS == "windows" {
			bin += ".exe"
		}
		build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-ldflags", testBuildLDFlags, "-o", bin, ".")
		build.Env = append(os.Environ(), "CGO_ENABLED=1")
		if out, err := build.CombinedOutput(); err != nil {
			derivedBinaryErr = fmt.Errorf("build gt-index: %v\n%s", err, out)
			return
		}
		derivedBinaryPath = bin
	})
	if derivedBinaryErr != nil {
		t.Fatal(derivedBinaryErr)
	}
	return derivedBinaryPath
}

// derivedFixtureCommits is the number of commits the fixture repository
// carries. Every commit touches all three files, so the co-change support of
// each of the three pairs equals this number exactly -- a count the assertions
// can name rather than approximate.
const derivedFixtureCommits = 6

// writeDerivedFixtureRepo materialises a three-file Python repository whose
// history couples every file to every other, whose call graph crosses a file
// boundary, and whose test file carries an assertion targeting the head of a
// two-hop certified call chain. It is the smallest repository on which all
// three derived layers have something true to say.
func writeDerivedFixtureRepo(t *testing.T, repo string) {
	t.Helper()
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	files := map[string]string{
		"mod.py":      "def leaf(value):\n    return value + 1\n\n\ndef middle(value):\n    return leaf(value) + 1\n\n\ndef entry(value):\n    return middle(value)\n",
		"caller.py":   "from mod import entry\n\n\ndef run(value):\n    return entry(value)\n",
		"test_mod.py": "from mod import entry\n\n\ndef test_entry_adds_two():\n    assert entry(1) == 3\n",
	}
	write := func(rev int) {
		for name, body := range files {
			content := body
			if rev > 0 {
				content = body + fmt.Sprintf("# rev %d\n", rev)
			}
			if err := os.WriteFile(filepath.Join(repo, name), []byte(content), 0o644); err != nil {
				t.Fatal(err)
			}
		}
	}
	git := func(args ...string) {
		full := append([]string{"-C", repo,
			"-c", "user.name=fixture",
			"-c", "user.email=fixture@example.invalid",
			"-c", "commit.gpgsign=false",
		}, args...)
		if out, err := exec.Command("git", full...).CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	write(0)
	git("init", "-q")
	git("add", "-A")
	git("commit", "-q", "-m", "commit 0")
	for rev := 1; rev < derivedFixtureCommits; rev++ {
		write(rev)
		git("add", "-A")
		git("commit", "-q", "-m", fmt.Sprintf("commit %d", rev))
	}
}

// indexDerivedFixture runs the real CLI over repo and returns an open handle
// on the published graph.
func indexDerivedFixture(t *testing.T, bin, repo, dbPath string, env ...string) *sql.DB {
	t.Helper()
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-workers", "2")
	index.Env = append(os.Environ(), env...)
	if out, err := index.CombinedOutput(); err != nil {
		t.Fatalf("full index: %v\n%s", err, out)
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func countDerivedRows(db *sql.DB, table string) (int, error) {
	var n int
	err := db.QueryRow("SELECT count(*) FROM " + table).Scan(&n)
	return n, err
}

func derivedMetaValue(db *sql.DB, key string) (string, error) {
	var v string
	err := db.QueryRow("SELECT value FROM project_meta WHERE key=?", key).Scan(&v)
	return v, err
}

// TestDerivedLayersArePublishedByTheRealCLI is the acceptance test for wiring
// the three landed analysis packages into publication. Each layer must leave
// rows AND a named outcome in project_meta. A named abstention -- a shallow
// clone, an absent history, a graph with no certified edges -- is an
// acceptable outcome; a missing table and a missing key are not, because they
// are indistinguishable from an analysis that never ran at all.
func TestDerivedLayersArePublishedByTheRealCLI(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	writeDerivedFixtureRepo(t, repo)
	db := indexDerivedFixture(t, buildDerivedIndexer(t), repo, filepath.Join(tmp, "graph.db"))

	for _, key := range []string{
		"derived_layers_state",
		"derived_cochange_state",
		"derived_cochange_pairs",
		"derived_community_state",
		"derived_community_count",
		"derived_community_cohesion",
		"derived_process_state",
		"derived_process_count",
		"derived_process_assertions_scanned",
	} {
		if _, err := derivedMetaValue(db, key); err != nil {
			t.Errorf("project_meta[%s]: %v", key, err)
		}
	}

	// The fixture is a full checkout with six multi-file commits, so co-change
	// has no licence to abstain: three pairs, each supported by every commit.
	if state, err := derivedMetaValue(db, "derived_cochange_state"); err == nil && state != "ok" {
		t.Errorf("derived_cochange_state = %q, want ok on a full checkout with history", state)
	}
	if pairs, err := countDerivedRows(db, "cochanges"); err != nil {
		t.Errorf("cochanges: %v", err)
	} else if pairs != 3 {
		t.Errorf("cochanges rows = %d, want 3", pairs)
	}
	var fullSupport int
	if err := db.QueryRow("SELECT count(*) FROM cochanges WHERE count=?", derivedFixtureCommits).Scan(&fullSupport); err != nil {
		t.Errorf("cochange support: %v", err)
	} else if fullSupport != 3 {
		t.Errorf("cochange pairs at full support = %d, want 3", fullSupport)
	}

	// Two cross-file CERTIFIED CALLS edges bind all three files into one
	// community, and its members must be recorded, not merely counted.
	if communities, err := countDerivedRows(db, "communities"); err != nil {
		t.Errorf("communities: %v", err)
	} else if communities < 1 {
		t.Errorf("communities rows = %d, want at least 1", communities)
	}
	if members, err := countDerivedRows(db, "community_members"); err != nil {
		t.Errorf("community_members: %v", err)
	} else if members < 2 {
		t.Errorf("community_members rows = %d, want at least 2", members)
	}

	// The assertion in test_mod.py targets entry(), which reaches leaf() over
	// two certified hops, so at least one witnessed process exists.
	if processes, err := countDerivedRows(db, "processes"); err != nil {
		t.Errorf("processes: %v", err)
	} else if processes < 1 {
		t.Errorf("processes rows = %d, want at least 1", processes)
	}
	if steps, err := countDerivedRows(db, "process_steps"); err != nil {
		t.Errorf("process_steps: %v", err)
	} else if steps < 2 {
		t.Errorf("process_steps rows = %d, want at least 2", steps)
	}
	var unwitnessed int
	if err := db.QueryRow("SELECT count(*) FROM processes WHERE witness_assertion_id IS NULL").Scan(&unwitnessed); err != nil {
		t.Errorf("unwitnessed processes: %v", err)
	} else if unwitnessed != 0 {
		t.Errorf("processes without a witnessing assertion = %d, want 0", unwitnessed)
	}
}

// TestDerivedLayersNeverPromoteCandidateEvidence pins the invariant the plan
// states in its global section: ranking, community membership and process
// membership never promote candidate evidence to verified evidence. It is
// checked the only way that cannot be argued with -- by publishing the same
// repository with and without the derived layers and comparing the core graph
// population for population.
func TestDerivedLayersNeverPromoteCandidateEvidence(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	writeDerivedFixtureRepo(t, repo)
	bin := buildDerivedIndexer(t)

	without := indexDerivedFixture(t, bin, repo, filepath.Join(tmp, "without.db"), "GT_DERIVED_LAYERS=off")
	with := indexDerivedFixture(t, bin, repo, filepath.Join(tmp, "with.db"))

	populations := []struct{ name, query string }{
		{"nodes", "SELECT count(*) FROM nodes"},
		{"edges", "SELECT count(*) FROM edges"},
		{"certified_edges", "SELECT count(*) FROM edges WHERE trust_tier='CERTIFIED'"},
		{"certified_calls", "SELECT count(*) FROM edges WHERE type='CALLS' AND trust_tier='CERTIFIED'"},
		{"candidate_edges", "SELECT count(*) FROM edges WHERE trust_tier='CANDIDATE'"},
		{"resolution_symbols", "SELECT count(*) FROM resolution_symbols"},
		{"assertions", "SELECT count(*) FROM assertions"},
	}
	for _, population := range populations {
		var before, after int
		if err := without.QueryRow(population.query).Scan(&before); err != nil {
			t.Fatalf("%s without the derived layers: %v", population.name, err)
		}
		if err := with.QueryRow(population.query).Scan(&after); err != nil {
			t.Fatalf("%s with the derived layers: %v", population.name, err)
		}
		if before != after {
			t.Errorf("%s changed when the derived layers ran: %d -> %d", population.name, before, after)
		}
	}

	// The switch must be observable in the receipt, not merely in the tables:
	// an operator who turned the layers off has to be able to prove it later.
	state, err := derivedMetaValue(without, "derived_layers_state")
	if err != nil {
		t.Fatalf("project_meta[derived_layers_state] with the layers off: %v", err)
	}
	if state != "disabled_by_operator" {
		t.Errorf("derived_layers_state under GT_DERIVED_LAYERS=off = %q, want disabled_by_operator", state)
	}
}
