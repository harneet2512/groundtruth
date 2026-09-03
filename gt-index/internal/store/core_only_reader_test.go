package store

import (
	"path/filepath"
	"strings"
	"testing"
)

// Publication is one all-or-nothing transaction, so a single candidate the
// store refuses to derive discards the whole index -- every file, symbol and
// structural edge the producer already paid to extract. Splitting it means a
// graph can legitimately reach disk carrying the core layer and no resolution
// evidence. Every reader that verifies the completion receipt then has to
// report that by name: today it dereferences a project_meta row a core-only
// graph never wrote and surfaces the raw SQLite miss, which reads as a corrupt
// graph rather than a degraded one.

func coreOnlyGraph(t *testing.T, state, reason string) *DB {
	t.Helper()
	db, err := Open(filepath.Join(t.TempDir(), "core-only.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if _, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"}); err != nil {
		t.Fatal(err)
	}
	if err := db.SetMeta("analysis_state", state); err != nil {
		t.Fatal(err)
	}
	if err := db.SetMeta("analysis_failure_reason", reason); err != nil {
		t.Fatal(err)
	}
	return db
}

func TestCoreOnlyGraphIsReportedAsAnalysisAbsentNotAsAMissingRow(t *testing.T) {
	db := coreOnlyGraph(t, "failed", "attach_graph_native_resolution_evidence")
	_, err := db.ResolveCandidates("callsite", ConservativeCandidatePolicy.VersionID(), "")
	if err == nil {
		t.Fatal("core-only graph was read as if it carried resolution evidence")
	}
	if !strings.Contains(err.Error(), "analysis_state=failed") {
		t.Fatalf("reader did not name the analysis state: %v", err)
	}
	if !strings.Contains(err.Error(), "attach_graph_native_resolution_evidence") {
		t.Fatalf("reader did not name the recorded failure reason: %v", err)
	}
	if strings.Contains(err.Error(), "no rows in result set") {
		t.Fatalf("reader leaked the raw storage miss instead of the analysis state: %v", err)
	}
}

func TestCoreOnlyGraphNamesAnalysisNotRunSeparatelyFromFailure(t *testing.T) {
	db := coreOnlyGraph(t, "not_run", "")
	_, err := db.QueryAttachedCandidates("caller")
	if err == nil {
		t.Fatal("core-only graph was read as if it carried resolution evidence")
	}
	if !strings.Contains(err.Error(), "analysis_state=not_run") {
		t.Fatalf("reader did not distinguish an unrun analysis: %v", err)
	}
	if strings.Contains(err.Error(), "no rows in result set") {
		t.Fatalf("reader leaked the raw storage miss instead of the analysis state: %v", err)
	}
}

// TestRejectedCandidateLeavesCoreGraphReadableAndNamed is the shape of the
// failure that aborted the paid gate run, reproduced at the transaction
// boundary: the core phase commits, the analysis phase rejects one candidate
// and rolls back as a unit. The structural graph must still be there, and the
// graph must say why the analysis is not.
func TestRejectedCandidateLeavesCoreGraphReadableAndNamed(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "two-phase.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	targetID, err := db.InsertNode(&Node{Label: "Function", Name: "target", QualifiedName: "target", FilePath: "main.py", Language: "python"})
	if err != nil {
		t.Fatal(err)
	}

	// Phase 1: the core graph commits on its own.
	coreTx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	structural := &Edge{SourceID: sourceID, TargetID: targetID, Type: "CALLS", SourceFile: "main.py", ResolutionMethod: "same_file", Confidence: 1.0, TrustTier: "CERTIFIED"}
	if err := BatchInsertEdgesTx(coreTx, []*Edge{structural}); err != nil {
		t.Fatal(err)
	}
	if err := coreTx.Commit(); err != nil {
		t.Fatal(err)
	}

	// Phase 2: one candidate the store refuses to derive (no native identity).
	analysisTx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	row := AttachedResolution{
		Source:   &Node{ID: sourceID, Label: "Function", Name: "caller", FilePath: "main.py", Language: "python"},
		Callsite: &ResolutionCallsite{CallsiteID: "rejected", SourceID: sourceID, SourceFile: "main.py", Callee: "target", DispatchState: "candidate_only", CandidateCount: 1, Mechanism: "name_match"},
		Candidates: []*ResolutionCandidate{
			{TargetID: targetID, TargetStableID: "target", TargetNativeID: "", Ordinal: 0, Mechanism: "name_match"},
		},
	}
	if err := AttachResolutionGraphTx(analysisTx, testGraphIdentity("rev"), []AttachedResolution{row}); err == nil {
		_ = analysisTx.Rollback()
		t.Fatal("a candidate with no native identity was published")
	}
	if err := analysisTx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if err := db.SetMeta("analysis_state", "failed"); err != nil {
		t.Fatal(err)
	}
	if err := db.SetMeta("analysis_failure_reason", "attach_graph_native_resolution_evidence"); err != nil {
		t.Fatal(err)
	}

	// The core graph is untouched by the analysis rollback.
	var calls int
	if err := db.db.QueryRow(`SELECT count(*) FROM edges WHERE type='CALLS'`).Scan(&calls); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("analysis rollback cost the core graph: %d CALLS edges survive, want 1", calls)
	}
	// And the analysis rolled back as a unit -- no half-written evidence.
	var callsites int
	if err := db.db.QueryRow(`SELECT count(*) FROM nodes WHERE label='Callsite'`).Scan(&callsites); err != nil {
		t.Fatal(err)
	}
	if callsites != 0 {
		t.Fatalf("rolled-back analysis left %d Callsite nodes", callsites)
	}
	// The reader must name the degraded state rather than report a missing row.
	if _, err := db.QueryAttachedCandidates("target"); err == nil {
		t.Fatal("core-only graph was read as if it carried resolution evidence")
	} else if !strings.Contains(err.Error(), "analysis_state=failed") {
		t.Fatalf("reader did not name the analysis state after a rejected candidate: %v", err)
	}
}
