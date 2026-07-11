package store

import (
	"os"
	"path/filepath"
	"testing"
)

// B-29 (composite revision): post_revision must be a COMPOSITE over ALL fact-producing
// surfaces — nodes, edges, properties, assertions, closure, cochange, content_fts — not
// just nodes+edges. A same-span property/assertion/closure/cochange/content change (that
// leaves the node+edge columns byte-identical) MUST move the revision, so content/property
// envelopes can be invalidated.
//
// RED on current code: ComputeRevision hashes only nodes+edges, so a property-only insert
// leaves the revision identical.

func b29SeedNode(t *testing.T, d *DB) {
	t.Helper()
	if _, err := d.db.Exec(
		`INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, signature, language)
		 VALUES (1,'Function','f','pkg.f','a.py',1,3,'def f()','python')`,
	); err != nil {
		t.Fatal(err)
	}
}

func TestB29_CompositeDetectsPropertyChange(t *testing.T) {
	d := b22TestDB(t)
	b29SeedNode(t, d)
	r0, err := d.ComputeRevision()
	if err != nil {
		t.Fatal(err)
	}
	// A property-only insert: no node column changes, no edge changes.
	if err := d.InsertProperty(&Property{NodeID: 1, Kind: "docstring", Value: "returns the user", Line: 2, Confidence: 1.0}); err != nil {
		t.Fatal(err)
	}
	r1, err := d.ComputeRevision()
	if err != nil {
		t.Fatal(err)
	}
	if r0 == r1 {
		t.Fatalf("property insert did not move the composite revision (properties not hashed): %q", r0)
	}
	// A property VALUE edit (same span) must also move it.
	if _, err := d.db.Exec(`UPDATE properties SET value = 'returns the admin user' WHERE node_id = 1`); err != nil {
		t.Fatal(err)
	}
	r2, _ := d.ComputeRevision()
	if r1 == r2 {
		t.Fatalf("same-span property value edit did not move the composite revision: %q", r1)
	}
}

func TestB29_CompositeDetectsAssertionChange(t *testing.T) {
	d := b22TestDB(t)
	b29SeedNode(t, d)
	r0, _ := d.ComputeRevision()
	if _, err := d.db.Exec(
		`INSERT INTO assertions (test_node_id, target_node_id, resolution_score, kind, expression, expected, line)
		 VALUES (1, 1, 0.9, 'assertEqual', 'f() == 1', '1', 2)`,
	); err != nil {
		t.Fatal(err)
	}
	r1, _ := d.ComputeRevision()
	if r0 == r1 {
		t.Fatalf("assertion insert did not move the composite revision (assertions not hashed): %q", r0)
	}
}

func TestB29_CompositeDetectsClosureAndCochangeChange(t *testing.T) {
	d := b22TestDB(t)
	b29SeedNode(t, d)
	if _, err := d.db.Exec(
		`INSERT INTO nodes (id,label,name,qualified_name,file_path,start_line,end_line,signature,language)
		 VALUES (2,'Function','g','pkg.g','b.py',1,3,'def g()','python')`,
	); err != nil {
		t.Fatal(err)
	}
	r0, _ := d.ComputeRevision()
	if err := d.BatchInsertClosure([]*Closure{{SourceID: 1, TargetID: 2, Depth: 1, MinConfidence: 1.0}}); err != nil {
		t.Fatal(err)
	}
	r1, _ := d.ComputeRevision()
	if r0 == r1 {
		t.Fatalf("closure insert did not move the composite revision (closure not hashed): %q", r0)
	}
	if err := d.BatchInsertCochanges(map[[2]string]int{{"a.py", "b.py"}: 3}); err != nil {
		t.Fatal(err)
	}
	r2, _ := d.ComputeRevision()
	if r1 == r2 {
		t.Fatalf("cochange insert did not move the composite revision (cochange not hashed): %q", r1)
	}
}

// B-29 content_fts sensitivity: a same-span BODY edit (node boundaries + signature
// unchanged) changes symbol_content_fts content and MUST move the composite revision.
func TestB29_CompositeDetectsContentFTSChange(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "svc.py"),
		[]byte("def alpha():\n    old_domain_term()\n    return 1\n"), 0o644); err != nil {
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
	if _, err := db.InsertNode(&Node{Label: "Function", Name: "alpha", FilePath: "svc.py", StartLine: 1, EndLine: 3, Language: "python"}); err != nil {
		t.Fatal(err)
	}
	if err := db.PopulateContentFTS(dir); err != nil {
		t.Fatal(err)
	}
	r0, _ := db.ComputeRevision()

	// Same-span body edit: change the body identifier but keep lines 1..3 boundaries.
	if err := os.WriteFile(filepath.Join(dir, "svc.py"),
		[]byte("def alpha():\n    new_domain_term()\n    return 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := db.PopulateContentFTS(dir); err != nil {
		t.Fatal(err)
	}
	r1, _ := db.ComputeRevision()
	if r0 == r1 {
		t.Fatalf("same-span body edit (content_fts changed) did not move the composite revision: %q", r0)
	}
}

// B-29 determinism: two identical builds → byte-identical composite revision.
func TestB29_CompositeDeterministic(t *testing.T) {
	build := func() string {
		d := b22TestDB(t)
		b29SeedNode(t, d)
		if err := d.InsertProperty(&Property{NodeID: 1, Kind: "guard_clause", Value: "if x", Line: 2, Confidence: 0.8}); err != nil {
			t.Fatal(err)
		}
		if _, err := d.db.Exec(
			`INSERT INTO assertions (test_node_id, target_node_id, resolution_score, kind, expression, expected, line)
			 VALUES (1,1,0.9,'assert','f()','', 2)`,
		); err != nil {
			t.Fatal(err)
		}
		if err := d.BatchInsertClosure([]*Closure{{SourceID: 1, TargetID: 1, Depth: 0, MinConfidence: 1.0}}); err != nil {
			t.Fatal(err)
		}
		r, err := d.ComputeRevision()
		if err != nil {
			t.Fatal(err)
		}
		return r
	}
	if a, b := build(), build(); a != b {
		t.Fatalf("composite revision not deterministic across two identical builds: %q != %q", a, b)
	}
}
