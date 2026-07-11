package store

import (
	"os"
	"path/filepath"
	"testing"
)

// B-23 (passage-level content identity): symbol_content_fts stores a whole body keyed by
// node rowid, so a body-concept match can only cite the node start_line, never the exact
// matching passage/line. A per-passage surface (content_passages: node_id, passage_id,
// start_line, end_line, content, content_hash + an FTS index) must let a body match cite
// the EXACT span.
//
// RED on current code: content_passages / content_passages_fts do not exist.
func TestB23_ContentPassagesCiteExactSpan(t *testing.T) {
	dir := t.TempDir()
	// alpha() body spans lines 1..4; the domain term "handshake" is ONLY on line 3.
	src := "def alpha():\n" + // 1
		"    x = 1\n" + // 2
		"    redis_tls_handshake(x)\n" + // 3
		"    return x\n" // 4
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
		t.Skip("FTS5 not compiled in (build without -tags sqlite_fts5)")
	}
	aID, err := db.InsertNode(&Node{Label: "Function", Name: "alpha", FilePath: "svc.py", StartLine: 1, EndLine: 4, Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	if err := db.PopulateContentFTS(dir); err != nil {
		t.Fatal(err)
	}

	// The passage table must exist and carry the exact span identity.
	var pTables int
	db.db.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='content_passages'").Scan(&pTables)
	if pTables == 0 {
		t.Fatalf("content_passages table missing — passages not stored (B-23 not implemented)")
	}

	// A body-concept match must resolve to the EXACT passage line (line 3), not the node
	// start_line (line 1). Query the passage FTS, join back to the passage span.
	var nodeID, startLine, endLine int64
	var content, contentHash string
	err = db.db.QueryRow(
		`SELECT p.node_id, p.start_line, p.end_line, p.content, p.content_hash
		   FROM content_passages_fts f
		   JOIN content_passages p ON p.passage_id = f.rowid
		  WHERE f.content MATCH 'handshake'
		  ORDER BY f.rank LIMIT 1`,
	).Scan(&nodeID, &startLine, &endLine, &content, &contentHash)
	if err != nil {
		t.Fatalf("passage FTS match for a body term returned no row: %v", err)
	}
	if nodeID != aID {
		t.Errorf("passage node_id=%d want %d", nodeID, aID)
	}
	if startLine != 3 {
		t.Errorf("passage cites start_line=%d, want the EXACT matching line 3 (not the node start_line 1)", startLine)
	}
	if endLine != 3 {
		t.Errorf("passage end_line=%d want 3", endLine)
	}
	if len(contentHash) != 64 {
		t.Errorf("content_hash is not a sha256 hex: %q", contentHash)
	}
	if content == "" {
		t.Errorf("passage content is empty")
	}
}

// B-23 test-hygiene: passages must inherit the is_test exclusion (a test body's assertion
// text is the strongest leak surface and is never an edit target).
func TestB23_ContentPassagesExcludeTests(t *testing.T) {
	dir := t.TempDir()
	src := "def test_flow():\n    assert connectRedisTLS()\n    return True\n"
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
		t.Skip("FTS5 not compiled in")
	}
	if _, err := db.InsertNode(&Node{Label: "Function", Name: "test_flow", FilePath: "test_svc.py", StartLine: 1, EndLine: 3, Language: "python", IsTest: true}); err != nil {
		t.Fatal(err)
	}
	if err := db.PopulateContentFTS(dir); err != nil {
		t.Fatal(err)
	}
	var pTables int
	db.db.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='content_passages'").Scan(&pTables)
	if pTables == 0 {
		t.Fatalf("content_passages table missing (B-23 not implemented)")
	}
	var cnt int
	db.db.QueryRow("SELECT COUNT(*) FROM content_passages").Scan(&cnt)
	if cnt != 0 {
		t.Errorf("content_passages has %d rows — a TEST body leaked into the passage index", cnt)
	}
}
