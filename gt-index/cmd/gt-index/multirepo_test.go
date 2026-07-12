package main

import (
	"database/sql"
	"os"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// buildRootList: single -root => len 1 (routes to the untouched single-root path);
// -roots adds de-duplicated additional roots after -root.
func TestBuildRootList(t *testing.T) {
	if got := buildRootList("/a", ""); len(got) != 1 || got[0] != "/a" {
		t.Fatalf("single root => %v, want [/a] (len 1 keeps the single-root path)", got)
	}
	got := buildRootList("/a", "/b, /c ,/a,/b")
	want := []string{"/a", "/b", "/c"}
	if len(got) != len(want) {
		t.Fatalf("buildRootList dedup => %v want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("buildRootList => %v want %v (order + dedup)", got, want)
		}
	}
}

func write(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// END-TO-END: two Go repos, repoA calls repoB's exported Add via a coordinate import.
// runMultiRepo must partition by repo_id, write the repos table, and mint a CERTIFIED
// cross-repo CALLS edge main->Add (and NOT to the unused Unused). The baseline binary
// on repoA alone cannot produce this (repoB isn't even indexed) — this is the GREEN.
func TestMultiRepo_GoCrossRepoEdge_EndToEnd(t *testing.T) {
	base := t.TempDir()
	repoA := filepath.Join(base, "repoa")
	repoB := filepath.Join(base, "repob")
	write(t, filepath.Join(repoB, "go.mod"), "module github.com/org/repob\n\ngo 1.21\n")
	write(t, filepath.Join(repoB, "mathlib", "add.go"),
		"package mathlib\n\nfunc Add(a, b int) int { return a + b }\n\nfunc Unused() int { return 0 }\n")
	write(t, filepath.Join(repoA, "go.mod"), "module github.com/org/repoa\n\ngo 1.21\n")
	write(t, filepath.Join(repoA, "main.go"),
		"package main\n\nimport \"github.com/org/repob/mathlib\"\n\nfunc main() {\n\t_ = mathlib.Add(1, 2)\n\t_ = localAdd()\n}\n\nfunc localAdd() int { return 0 }\n")

	out := filepath.Join(base, "graph.db")
	t.Setenv("GT_INDEX_FIXED_TS", "2026-01-01T00:00:00Z")
	if err := runMultiRepo([]string{repoA, repoB}, out, 10000, 1); err != nil {
		t.Fatalf("runMultiRepo: %v", err)
	}

	// Repos table has both roots.
	db, err := store.Open(out)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if got := db.RepoCount(); got != 2 {
		t.Fatalf("RepoCount=%d want 2", got)
	}
	db.Close()

	// Query the cross-repo edge directly.
	raw, err := sql.Open("sqlite3", out)
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()

	var (
		srcName, tgtName, method, tier, meta string
		conf                                 float64
		repoID                               sql.NullInt64
	)
	err = raw.QueryRow(`
		SELECT ns.name, nt.name, e.resolution_method, e.trust_tier, e.confidence, e.metadata, e.repo_id
		  FROM edges e
		  JOIN nodes ns ON ns.id = e.source_id
		  JOIN nodes nt ON nt.id = e.target_id
		 WHERE e.resolution_method LIKE '%cross_repo%'`).
		Scan(&srcName, &tgtName, &method, &tier, &conf, &meta, &repoID)
	if err != nil {
		t.Fatalf("no cross-repo edge found: %v", err)
	}
	if srcName != "main" || tgtName != "Add" {
		t.Errorf("cross-repo edge %s->%s want main->Add", srcName, tgtName)
	}
	if method != "call_cross_repo" || tier != "CERTIFIED" || conf < 0.9 {
		t.Errorf("cross-repo edge trust=%s/%s/%.2f want call_cross_repo/CERTIFIED/>=0.9", method, tier, conf)
	}
	if !repoID.Valid || repoID.Int64 != 0 {
		t.Errorf("cross-repo edge repo_id=%v want 0 (source repo)", repoID)
	}
	if !contains(meta, "target_repo_id=1") || !contains(meta, "resolved_by=go_module") {
		t.Errorf("cross-repo edge metadata %q missing repo provenance", meta)
	}

	// DROP: no cross-repo edge to Unused (repoA only calls Add).
	var toUnused int
	if err := raw.QueryRow(`
		SELECT COUNT(*) FROM edges e JOIN nodes n ON n.id = e.target_id
		 WHERE e.resolution_method LIKE '%cross_repo%' AND n.name = 'Unused'`).Scan(&toUnused); err != nil {
		t.Fatal(err)
	}
	if toUnused != 0 {
		t.Errorf("laundered edge to unused symbol Unused (%d) — only the CALLED symbol must link", toUnused)
	}

	// repo_id partitioning: repoB's Add is repo 1, repoA's main is repo 0.
	var addRepo, mainRepo int64
	raw.QueryRow(`SELECT repo_id FROM nodes WHERE name='Add'`).Scan(&addRepo)
	raw.QueryRow(`SELECT repo_id FROM nodes WHERE name='main'`).Scan(&mainRepo)
	if addRepo != 1 || mainRepo != 0 {
		t.Errorf("repo_id partition wrong: Add=%d(want1) main=%d(want0)", addRepo, mainRepo)
	}

	// schema_version is the bumped multi-repo value (fail-closes single-root-only readers).
	var sv string
	raw.QueryRow(`SELECT value FROM project_meta WHERE key='schema_version'`).Scan(&sv)
	if sv != schemaVersionMultiRepo {
		t.Errorf("multi-repo schema_version=%q want %q", sv, schemaVersionMultiRepo)
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
