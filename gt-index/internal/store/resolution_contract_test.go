package store

import (
	"path/filepath"
	"testing"
)

func TestResolutionStableIdentityIsDeterministic(t *testing.T) {
	n := Node{Language: "python", FilePath: "src/a.py", QualifiedName: "a.run", Label: "Function", StartLine: 2, EndLine: 4}
	a := StableResolutionSymbolID(n)
	if a == "" || a != StableResolutionSymbolID(n) {
		t.Fatalf("unstable symbol identity: %q", a)
	}
	c := StableResolutionCallsiteID("rev", a, "src/a.py", 3, 3, "run")
	if c == "" || c != StableResolutionCallsiteID("rev", a, "src/a.py", 3, 3, "run") {
		t.Fatalf("unstable callsite identity: %q", c)
	}
}

func TestAttachResolutionGraphRejectsCountDivergenceTransactionally(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	row := AttachedResolution{
		Source:   &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"},
		Callsite: &ResolutionCallsite{CallsiteID: "cs", SourceID: sourceID, SourceFile: "main.py", SourceLine: 3, Callee: "target", DispatchState: "unique", CandidateCount: 1, Mechanism: "same_file"},
	}
	if err := AttachResolutionGraphTx(tx, "rev", []AttachedResolution{row}); err == nil {
		t.Fatal("expected count/retained-row invariant failure")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	var nodes, complete int
	if err := db.db.QueryRow("SELECT count(*) FROM nodes WHERE label='Callsite'").Scan(&nodes); err != nil {
		t.Fatal(err)
	}
	if nodes != 0 {
		t.Fatalf("failed transaction published %d callsite nodes", nodes)
	}
	if err := db.db.QueryRow("SELECT count(*) FROM project_meta WHERE key='graph_resolution_complete'").Scan(&complete); err != nil {
		t.Fatal(err)
	}
	if complete != 0 {
		t.Fatal("failed transaction published graph completion metadata")
	}
}
