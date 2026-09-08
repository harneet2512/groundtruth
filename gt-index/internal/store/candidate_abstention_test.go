package store

import (
	"path/filepath"
	"testing"
)

// A derivation-invalid candidate is an abstention: the producer has no
// publishable facts for it. It must be dropped from the graph without taking
// the rest of the successfully derived resolution evidence down with it.
func TestAttachResolutionGraphSkipsDerivationInvalidCandidateAsAbstention(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	derivableTargetID, err := db.InsertNode(&Node{Label: "Method", Name: "Run", QualifiedName: "ImplA.Run", FilePath: "a.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	abstainedTargetID, err := db.InsertNode(&Node{Label: "Method", Name: "Run", QualifiedName: "ImplB.Run", FilePath: "b.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	const sourceFactID = "vta_source_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	const edgeFactID = "vta_edge_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	row := AttachedResolution{
		Source: &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"},
		Callsite: &ResolutionCallsite{
			CallsiteID: "vta-abstention", SourceID: sourceID, SourceFile: "main.go", SourceLine: 8,
			Callee: "Run", DispatchState: "ambiguous", DispatchForm: "interface", CandidateCount: 2, Mechanism: "vta",
			PassCoverage: []ResolutionPassCoverage{{PassKind: "vta", Status: "partial", Reason: "candidate_only_flow_evidence"}},
		},
		Candidates: []*ResolutionCandidate{
			{
				TargetID: derivableTargetID, TargetStableID: "a", TargetNativeID: "a", Ordinal: 0, Mechanism: "vta",
				FlowSourceStableIDs: []string{sourceFactID}, FlowEdgeStableIDs: []string{edgeFactID},
				FlowSourceFacts: []ResolutionFlowFact{{StableID: sourceFactID, Kind: "source", CallsiteID: "vta-abstention", TargetStableID: "a", Payload: "runner=ImplA"}},
				FlowEdgeFacts:   []ResolutionFlowFact{{StableID: edgeFactID, Kind: "edge", CallsiteID: "vta-abstention", TargetStableID: "a", Payload: "caller->runner"}},
			},
			// No typed source or propagation facts: variable_type_flow cannot be
			// derived for this candidate, so it must be abstained, not aborted on.
			{TargetID: abstainedTargetID, TargetStableID: "b", TargetNativeID: "b", Ordinal: 1, Mechanism: "vta"},
		},
	}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err != nil {
		t.Fatalf("derivation-invalid candidate aborted the whole resolution graph: %v", err)
	}

	evidence, err := db.QueryAttachedCandidates("Run")
	if err != nil {
		t.Fatal(err)
	}
	if len(evidence) != 1 || evidence[0].TargetStableID != "a" || evidence[0].TargetID != derivableTargetID {
		t.Fatalf("expected only the derivable candidate to be published, got %+v", evidence)
	}

	var abstainedEdges int
	if err := db.db.QueryRow(`SELECT COUNT(*) FROM edges WHERE target_id=? AND type IN ('CANDIDATE','CANDIDATE_TARGET')`, abstainedTargetID).Scan(&abstainedEdges); err != nil {
		t.Fatal(err)
	}
	if abstainedEdges != 0 {
		t.Fatalf("abstained candidate was still published as %d candidate edges", abstainedEdges)
	}

	skipped, recorded, err := db.GraphSkippedCandidates()
	if err != nil {
		t.Fatal(err)
	}
	if !recorded || skipped != 1 {
		t.Fatalf("graph receipt skip counter = (%d, recorded=%t), want (1, true)", skipped, recorded)
	}
}

// A build that drops nothing still records the counter, so a consumer can tell
// "no abstentions" apart from "produced before the counter existed".
func TestAttachResolutionGraphRecordsZeroSkippedCandidates(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	targetID, err := db.InsertNode(&Node{Label: "Method", Name: "Run", QualifiedName: "ImplA.Run", FilePath: "a.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	selected := "a"
	row := AttachedResolution{
		Source: &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"},
		Callsite: &ResolutionCallsite{
			CallsiteID: "same-file", SourceID: sourceID, SourceFile: "main.go", SourceLine: 8,
			Callee: "Run", DispatchState: "unique", DispatchForm: "static", CandidateCount: 1, Mechanism: "same_file",
			SelectedTargetStableID: &selected, SelectedTargetNativeID: &selected,
		},
		Candidates: []*ResolutionCandidate{{TargetID: targetID, TargetStableID: "a", TargetNativeID: "a", Ordinal: 0, Mechanism: "same_file", Selected: true}},
	}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err != nil {
		t.Fatal(err)
	}
	skipped, recorded, err := db.GraphSkippedCandidates()
	if err != nil {
		t.Fatal(err)
	}
	if !recorded || skipped != 0 {
		t.Fatalf("graph receipt skip counter = (%d, recorded=%t), want (0, true)", skipped, recorded)
	}
}
