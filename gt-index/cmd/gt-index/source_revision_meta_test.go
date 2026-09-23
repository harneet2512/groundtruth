package main

// source_revision_meta_test.go — the graph source-revision identity contract
// (HAR-90 CANON): project_meta.git_commit stays the PRODUCER build commit;
// the indexed workspace revision the caller passes with -source-revision is
// written to project_meta.source_revision on a full build, a batch amend
// (-amend-parent) and a per-file amend (-file). Without the flag the key is
// ABSENT — never a parent's stale value — and the binary advertises the
// behaviour as build capability source_revision_meta_v1.

import (
	"database/sql"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestBuildDeclaresSourceRevisionMetaCapability(t *testing.T) {
	for _, capability := range declaredBuildIdentity().Capabilities {
		if capability == "source_revision_meta_v1" {
			return
		}
	}
	t.Fatalf("capability source_revision_meta_v1 missing from %v", declaredBuildIdentity().Capabilities)
}

// sourceRevisionMeta returns (value, present).
func sourceRevisionMeta(t *testing.T, path string) (string, bool) {
	t.Helper()
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var v string
	err = db.QueryRow(`SELECT value FROM project_meta WHERE key='source_revision'`).Scan(&v)
	if err == sql.ErrNoRows {
		return "", false
	}
	if err != nil {
		t.Fatal(err)
	}
	return v, true
}

func producerCommitMeta(t *testing.T, path string) string {
	t.Helper()
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var v string
	if err := db.QueryRow(`SELECT value FROM project_meta WHERE key='git_commit'`).Scan(&v); err != nil {
		t.Fatal(err)
	}
	return v
}

func TestSourceRevisionMetaOnFullBatchAndFileBuilds(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	bin := buildDerivedIndexer(t)
	root := t.TempDir()
	repo := filepath.Join(root, "repo")
	writeDerivedFixtureRepo(t, repo)
	env := []string{"GT_PARSE_CACHE_ROOT=" + filepath.Join(root, "cache")}
	run := func(args ...string) {
		t.Helper()
		cmd := exec.Command(bin, args...)
		cmd.Env = append(os.Environ(), env...)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("gt-index %v: %v\n%s", args, err, out)
		}
	}
	edit := func(marker string) {
		t.Helper()
		p := filepath.Join(repo, "mod.py")
		src, err := os.ReadFile(p)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(string(src)+"\n# "+marker+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	// Full build with the flag.
	full := filepath.Join(root, "full.db")
	run("-root", repo, "-output", full, "-source-revision", "ws-rev-1")
	if v, ok := sourceRevisionMeta(t, full); !ok || v != "ws-rev-1" {
		t.Fatalf("full build source_revision=%q present=%v, want ws-rev-1", v, ok)
	}
	if got := producerCommitMeta(t, full); got != "test-commit" {
		t.Fatalf("git_commit must stay the producer build commit, got %q", got)
	}

	// Full build without the flag: absent, never inferred.
	bare := filepath.Join(root, "bare.db")
	run("-root", repo, "-output", bare)
	if v, ok := sourceRevisionMeta(t, bare); ok {
		t.Fatalf("full build without -source-revision wrote source_revision=%q", v)
	}

	// Batch amend with the flag replaces the parent's value.
	edit("amend one")
	amended := filepath.Join(root, "amended.db")
	run("-root", repo, "-output", amended, "-amend-parent", full, "-source-revision", "ws-rev-2")
	if v, ok := sourceRevisionMeta(t, amended); !ok || v != "ws-rev-2" {
		t.Fatalf("batch amend source_revision=%q present=%v, want ws-rev-2", v, ok)
	}
	// Batch amend without the flag must not carry the parent's (now stale) value.
	edit("amend two")
	unflagged := filepath.Join(root, "unflagged.db")
	run("-root", repo, "-output", unflagged, "-amend-parent", amended)
	if v, ok := sourceRevisionMeta(t, unflagged); ok {
		t.Fatalf("batch amend without -source-revision carried the parent's source_revision=%q", v)
	}

	// Per-file amend with the flag.
	perFile := filepath.Join(root, "perfile.db")
	copyConvergenceGraph(t, amended, perFile)
	edit("file one")
	run("-root", repo, "-output", perFile, "-file", "mod.py", "-source-revision", "ws-rev-3")
	if v, ok := sourceRevisionMeta(t, perFile); !ok || v != "ws-rev-3" {
		t.Fatalf("-file source_revision=%q present=%v, want ws-rev-3", v, ok)
	}
	// Per-file amend without the flag removes the now-stale value.
	edit("file two")
	run("-root", repo, "-output", perFile, "-file", "mod.py")
	if v, ok := sourceRevisionMeta(t, perFile); ok {
		t.Fatalf("-file without -source-revision left a stale source_revision=%q", v)
	}
	// A short-circuited -file (content unchanged) still rebinds the identity:
	// the graph content IS the content of the named revision.
	run("-root", repo, "-output", perFile, "-file", "mod.py", "-source-revision", "ws-rev-4")
	if v, ok := sourceRevisionMeta(t, perFile); !ok || v != "ws-rev-4" {
		t.Fatalf("short-circuited -file source_revision=%q present=%v, want ws-rev-4", v, ok)
	}
}

func TestSourceRevisionFlagRejectsControlCharacters(t *testing.T) {
	for _, bad := range []string{"a\nb", "tab\there", "nul\x00"} {
		if err := validateSourceRevision(bad); err == nil {
			t.Errorf("validateSourceRevision(%q) accepted a control character", bad)
		}
	}
	for _, good := range []string{"", "0becde10", "sha256:" + strings.Repeat("a", 64)} {
		if err := validateSourceRevision(good); err != nil {
			t.Errorf("validateSourceRevision(%q) = %v", good, err)
		}
	}
}
