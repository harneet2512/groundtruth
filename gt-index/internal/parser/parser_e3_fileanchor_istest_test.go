package parser

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// E3 (Fable 2026-07-05): a tests/ barrel (re-export __init__.py / index.ts) yields ZERO symbol
// nodes, so only the synthetic File anchor is emitted for it. Before the fix the anchor never
// carried is_test → the test file entered the graph as PRODUCTION and became an FTS-seedable test
// PATH. The walker's file-level isTest already knows the truth; the fix threads it into the anchor.
//
// RED (revert the isTest thread): the File anchor has IsTest=false.
func TestFileAnchor_TestBarrelCarriesIsTest(t *testing.T) {
	dir := t.TempDir()
	// A tests/ re-export barrel: zero symbols, a line-start re-export → File anchor path.
	path := filepath.Join(dir, "index.ts")
	if err := os.WriteFile(path, []byte("export * from \"./fixtures\";\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	spec := specs.ForExtension(".ts")
	if spec == nil {
		t.Skip("no ts spec")
	}
	// Path mimics a test tree; the WALKER decides isTest — here we pass the test truth directly.
	sf := walker.SourceFile{Path: "tests/index.ts", AbsPath: path, Language: spec.Name, Spec: spec}
	res, err := ParseFile(sf, true) // isTest=true, as the walker would classify a tests/ file
	if err != nil {
		t.Fatalf("ParseFile: %v", err)
	}
	var found bool
	for _, n := range res.Nodes {
		if n.Label == "File" {
			found = true
			if !n.IsTest {
				t.Errorf("File anchor for a tests/ barrel has IsTest=false — it will seed nodes_fts "+
					"as a production path (leak surface); want IsTest=true")
			}
		}
	}
	if !found {
		t.Fatal("barrel file lost its File anchor (harness broken)")
	}
}

// Guard: a PRODUCTION barrel anchor must stay is_test=false (the thread must not over-flag).
func TestFileAnchor_ProdBarrelStaysNonTest(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "index.ts")
	if err := os.WriteFile(path, []byte("export * from \"./foo\";\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	spec := specs.ForExtension(".ts")
	if spec == nil {
		t.Skip("no ts spec")
	}
	sf := walker.SourceFile{Path: "src/index.ts", AbsPath: path, Language: spec.Name, Spec: spec}
	res, err := ParseFile(sf, false)
	if err != nil {
		t.Fatalf("ParseFile: %v", err)
	}
	for _, n := range res.Nodes {
		if n.Label == "File" && n.IsTest {
			t.Errorf("production barrel anchor wrongly flagged IsTest=true")
		}
	}
}
