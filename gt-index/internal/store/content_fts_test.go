package store

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// B1 tokenizer: BLUiR sub-token splitting (camelCase + snake_case), lowercased,
// frequency preserved, 1-char tokens dropped.
func TestContentTokens_SubTokenSplitting(t *testing.T) {
	got := contentTokens("connectRedisTLS  redis_tls_handshake")
	toks := map[string]bool{}
	for _, tk := range strings.Fields(got) {
		toks[tk] = true
	}
	for _, want := range []string{"connect", "redis", "tls", "handshake", "connectredistls", "redis_tls_handshake"} {
		if !toks[want] {
			t.Errorf("contentTokens missing %q; got %q", want, got)
		}
	}
	// acronym boundary: "HTTPServer" -> http, server
	got2 := contentTokens("HTTPServer")
	if !strings.Contains(" "+got2+" ", " http ") || !strings.Contains(" "+got2+" ", " server ") {
		t.Errorf("acronym split failed: %q", got2)
	}
}

// B1 end-to-end: a BODY-ONLY domain term (present in the function body, ABSENT from
// the node name/signature) must retrieve that symbol from symbol_content_fts — the
// stratum-B point the name-only surfaces (nodes_fts, embedder, anchor) structurally miss.
func TestPopulateContentFTS_RetrievesByBodyNotName(t *testing.T) {
	dir := t.TempDir()
	src := "def alpha():\n" + // 1
		"    redis_tls_handshake()\n" + // 2
		"    return 1\n" + // 3
		"\n" + // 4
		"\n" + // 5
		"def beta():\n" + // 6
		"    unrelated_widget_paint()\n" + // 7
		"    return 2\n" // 8
	if err := os.WriteFile(filepath.Join(dir, "svc.py"), []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}

	db, err := Open(filepath.Join(dir, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.EnsureContentFTS(); err != nil {
		t.Fatal(err)
	}
	var exists int
	db.db.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='symbol_content_fts'").Scan(&exists)
	if exists == 0 {
		t.Skip("FTS5 not compiled in (build without -tags sqlite_fts5) — content surface unavailable")
	}

	alpha := &Node{Label: "Function", Name: "alpha", FilePath: "svc.py", StartLine: 1, EndLine: 3, Language: "python"}
	beta := &Node{Label: "Function", Name: "beta", FilePath: "svc.py", StartLine: 6, EndLine: 8, Language: "python"}
	aID, err := db.InsertNode(alpha)
	if err != nil {
		t.Fatal(err)
	}
	bID, err := db.InsertNode(beta)
	if err != nil {
		t.Fatal(err)
	}

	if err := db.PopulateContentFTS(dir); err != nil {
		t.Fatal(err)
	}

	// "redis" appears in alpha's BODY only (not in the name "alpha") -> retrieves alpha.
	assertMatch := func(term string, wantID int64) {
		t.Helper()
		var gotID int64
		err := db.db.QueryRow(
			"SELECT rowid FROM symbol_content_fts WHERE content MATCH ? ORDER BY rank LIMIT 1", term,
		).Scan(&gotID)
		if err != nil {
			t.Fatalf("MATCH %q returned no row (body content not indexed?): %v", term, err)
		}
		if gotID != wantID {
			t.Errorf("MATCH %q -> node %d, want %d", term, gotID, wantID)
		}
	}
	assertMatch("redis", aID)     // body-only term of alpha
	assertMatch("handshake", aID) // body-only term of alpha
	assertMatch("widget", bID)    // body-only term of beta

	// a name-absent term must NOT spuriously match the other symbol
	var cnt int
	db.db.QueryRow("SELECT COUNT(*) FROM symbol_content_fts WHERE content MATCH 'redis'").Scan(&cnt)
	if cnt != 1 {
		t.Errorf("MATCH 'redis' hit %d symbols, want exactly 1 (alpha)", cnt)
	}
}

// B1 leak-safety: a TEST symbol's body (assertion text, FAIL_TO_PASS test names) is the
// strongest lexical echo of an issue and is NEVER an edit target — it must not enter the
// content index. Excluded at the SOURCE (is_test=0 in PopulateContentFTS).
func TestPopulateContentFTS_ExcludesTestSymbols(t *testing.T) {
	dir := t.TempDir()
	src := "def test_redis_flow():\n" + // 1
		"    assert connectRedisTLS()\n" + // 2
		"    return True\n" // 3
	if err := os.WriteFile(filepath.Join(dir, "test_svc.py"), []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	db, err := Open(filepath.Join(dir, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.EnsureContentFTS(); err != nil {
		t.Fatal(err)
	}
	var exists int
	db.db.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='symbol_content_fts'").Scan(&exists)
	if exists == 0 {
		t.Skip("FTS5 not compiled in — content surface unavailable")
	}
	tnode := &Node{Label: "Function", Name: "test_redis_flow", FilePath: "test_svc.py",
		StartLine: 1, EndLine: 3, Language: "python", IsTest: true}
	if _, err := db.InsertNode(tnode); err != nil {
		t.Fatal(err)
	}
	if err := db.PopulateContentFTS(dir); err != nil {
		t.Fatal(err)
	}
	var cnt int
	db.db.QueryRow("SELECT COUNT(*) FROM symbol_content_fts WHERE content MATCH 'redis'").Scan(&cnt)
	if cnt != 0 {
		t.Errorf("MATCH 'redis' hit %d rows — a TEST symbol's body leaked into the content index", cnt)
	}
}

// LIPI BUG 1 / Fable #3 GUARD: an incremental reindex deletes a file's old nodes and
// inserts new ones with FRESH ids (AUTOINCREMENT never reuses). RepopulateContentFTSForFile
// must sweep the old ids' content rows, not leave them orphaned (space leak + bm25 IDF
// drift → a graph reached incrementally would rank differently from a byte-fresh build).
// RED before the orphan-sweep (DELETE ... WHERE rowid NOT IN (SELECT id FROM nodes)).
func TestRepopulateContentFTS_SweepsOrphansAfterIdChange(t *testing.T) {
	dir := t.TempDir()
	src := "def alpha():\n    redis_connect_handshake()\n    return 1\n"
	if err := os.WriteFile(filepath.Join(dir, "svc.py"), []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	db, err := Open(filepath.Join(dir, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.EnsureContentFTS(); err != nil {
		t.Fatal(err)
	}
	var exists int
	db.db.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='symbol_content_fts'").Scan(&exists)
	if exists == 0 {
		t.Skip("FTS5 not compiled in — content surface unavailable")
	}

	// Initial index: node for svc.py::alpha, content populated.
	id1, err := db.InsertNode(&Node{Label: "Function", Name: "alpha", FilePath: "svc.py", StartLine: 1, EndLine: 3, Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	if err := db.PopulateContentFTS(dir); err != nil {
		t.Fatal(err)
	}

	// Reindex: delete node id1, re-insert alpha → a FRESH id (AUTOINCREMENT > id1, never
	// reused), then run the content refresh.
	if _, err := db.db.Exec("DELETE FROM nodes WHERE id = ?", id1); err != nil {
		t.Fatal(err)
	}
	id2, err := db.InsertNode(&Node{Label: "Function", Name: "alpha", FilePath: "svc.py", StartLine: 1, EndLine: 3, Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	if id2 == id1 {
		t.Fatalf("AUTOINCREMENT reused id %d — test premise broken", id1)
	}
	if err := db.RepopulateContentFTSForFile(dir, "svc.py"); err != nil {
		t.Fatal(err)
	}

	var orphans int
	db.db.QueryRow("SELECT COUNT(*) FROM symbol_content_fts WHERE rowid NOT IN (SELECT id FROM nodes)").Scan(&orphans)
	if orphans != 0 {
		t.Errorf("orphan content rows = %d (want 0) — the delete-by-current-id was a no-op; stale ids accumulate every reindex", orphans)
	}
	var live int
	db.db.QueryRow("SELECT COUNT(*) FROM symbol_content_fts WHERE rowid = ?", id2).Scan(&live)
	if live != 1 {
		t.Errorf("live node %d content rows = %d (want 1)", id2, live)
	}
}
