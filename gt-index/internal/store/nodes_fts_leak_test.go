package store

import (
	"path/filepath"
	"testing"
)

// LEAK INVARIANT (Fable S1/L2, reproduced on the live artifact): nodes_fts is the
// localizer's stage-1 retrieval surface. A test symbol indexed here becomes an
// FTS5_SEED / BFS root and renders agent-visibly as `fts5 match: …` — surfacing test
// NAMES (Go `TestX`, Mocha `it: …`), the FAIL_TO_PASS surface. PopulateFTS5 must
// exclude is_test at INSERT, parity with PopulateContentFTS. RED before the WHERE
// filter (the test node matches `redis`); GREEN after (only the production node).
func TestPopulateFTS5_ExcludesTestSymbols(t *testing.T) {
	dir := t.TempDir()
	db, err := Open(filepath.Join(dir, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	var exists int
	db.db.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='nodes_fts'").Scan(&exists)
	if exists == 0 {
		t.Skip("FTS5 not compiled in (build without -tags sqlite_fts5) — nodes_fts unavailable")
	}

	// A production node and a TEST node that share the standalone token 'redis'
	// (unicode61 splits handle_redis -> handle, redis and test_redis -> test, redis).
	prod := &Node{Label: "Function", Name: "handle_redis", FilePath: "svc/net.go", StartLine: 1, EndLine: 3, Language: "go"}
	test := &Node{Label: "Function", Name: "test_redis", FilePath: "svc/net_test.go", StartLine: 1, EndLine: 3, Language: "go", IsTest: true}
	if _, err := db.InsertNode(prod); err != nil {
		t.Fatal(err)
	}
	if _, err := db.InsertNode(test); err != nil {
		t.Fatal(err)
	}
	if err := db.PopulateFTS5(); err != nil {
		t.Fatal(err)
	}

	// Exactly one row must match 'redis' — the production node. If the test node were
	// indexed (pre-fix) this would be 2, and its name would be in the seed surface.
	var hits int
	if err := db.db.QueryRow("SELECT COUNT(*) FROM nodes_fts WHERE nodes_fts MATCH 'redis'").Scan(&hits); err != nil {
		t.Fatalf("MATCH 'redis' failed: %v", err)
	}
	if hits != 1 {
		t.Errorf("MATCH 'redis' on nodes_fts hit %d rows, want exactly 1 (the production node) — a test symbol leaked into the retrieval surface", hits)
	}

	var name string
	if err := db.db.QueryRow("SELECT name FROM nodes_fts WHERE nodes_fts MATCH 'redis' LIMIT 1").Scan(&name); err != nil {
		t.Fatalf("fetch surviving row failed: %v", err)
	}
	if name != "handle_redis" {
		t.Errorf("surviving nodes_fts row = %q, want the production node handle_redis", name)
	}
}
