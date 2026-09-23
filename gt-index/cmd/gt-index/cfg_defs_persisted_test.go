package main

import (
	"os/exec"
	"path/filepath"
	"reflect"
	"testing"
)

// TestGoShortVarDeclDefsPersisted: HAR-90 F15 at the table level. The Go
// fixture's Compute defines y ONLY via `y := Helper(a)`; before the parser
// fix cfg_defs held just the parameters, so a persisted-CFG slice at
// `return y + b` lost the data dependence on the Helper call.
func TestGoShortVarDeclDefsPersisted(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	bin := buildDerivedIndexer(t)
	root := t.TempDir()
	repo := filepath.Join(root, "repo")
	writeConvergenceRepo(t, repo, convergenceFixtures()[1])
	graph := filepath.Join(root, "graph.db")
	if out, err := exec.Command(bin, "-root", repo, "-output", graph).CombinedOutput(); err != nil {
		t.Fatalf("index: %v\n%s", err, out)
	}
	defs := batchQueryRows(t, graph, `SELECT d.var_name, d.line FROM cfg_defs d JOIN nodes n ON n.id=d.node_id
		WHERE n.file_path='core/core.go' AND n.name='Compute' ORDER BY d.line, d.var_name`)
	want := []string{"[a 7]", "[b 7]", "[y 8]"}
	if !reflect.DeepEqual(defs, want) {
		t.Fatalf("Compute cfg_defs = %v, want %v", defs, want)
	}
	uses := batchQueryRows(t, graph, `SELECT u.var_name FROM cfg_uses u JOIN nodes n ON n.id=u.node_id
		WHERE n.file_path='core/core.go' AND n.name='Compute' AND u.line=9`)
	if !reflect.DeepEqual(uses, []string{"[b]", "[y]"}) {
		t.Fatalf("Compute return-line uses = %v, want y and b (the def-use chain y:8 -> 9)", uses)
	}
}
