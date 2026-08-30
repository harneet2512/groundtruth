package store

import (
	"encoding/json"
	"path/filepath"
	"testing"
)

func TestVTACandidateProvenanceCarriesSourceAndEdgeFacts(t *testing.T) {
	candidate := &ResolutionCandidate{
		TargetID:            7,
		TargetStableID:      "target",
		TargetNativeID:      "target-native",
		Mechanism:           "vta",
		FlowSourceStableIDs: []string{"assign:runner"},
		FlowEdgeStableIDs:   []string{"edge:caller->runner"},
	}
	steps := candidateProvenance(nil, candidate)
	got := make(map[string]string)
	for _, step := range steps {
		got[step.Kind] = step.Value
	}
	for kind, want := range map[string][]string{
		"flow_source_stable_ids": {"assign:runner"},
		"flow_edge_stable_ids":   {"edge:caller->runner"},
	} {
		var values []string
		if err := json.Unmarshal([]byte(got[kind]), &values); err != nil {
			t.Fatalf("%s was not serialized as typed IDs: %q: %v", kind, got[kind], err)
		}
		if len(values) != len(want) || values[0] != want[0] {
			t.Fatalf("%s=%v, want %v", kind, values, want)
		}
	}
}

func TestAttachResolutionGraphPersistsPerCandidateVTAFacts(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	targetA, err := db.InsertNode(&Node{Label: "Method", Name: "Run", QualifiedName: "ImplA.Run", FilePath: "a.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	targetB, err := db.InsertNode(&Node{Label: "Method", Name: "Run", QualifiedName: "ImplB.Run", FilePath: "b.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	row := AttachedResolution{
		Source:   &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"},
		Callsite: &ResolutionCallsite{CallsiteID: "vta-facts", SourceID: sourceID, SourceFile: "main.go", SourceLine: 8, Callee: "Run", DispatchState: "ambiguous", DispatchForm: "interface", CandidateCount: 2, Mechanism: "vta", PassCoverage: []ResolutionPassCoverage{{PassKind: "vta", Status: "partial", Reason: "candidate_only_flow_evidence"}}},
		Candidates: []*ResolutionCandidate{
			{TargetID: targetA, TargetStableID: "a", TargetNativeID: "a", Ordinal: 0, Mechanism: "vta", FlowSourceStableIDs: []string{"vta_source_a"}, FlowEdgeStableIDs: []string{"vta_edge_a"}},
			{TargetID: targetB, TargetStableID: "b", TargetNativeID: "b", Ordinal: 1, Mechanism: "vta", FlowSourceStableIDs: []string{"vta_source_b"}, FlowEdgeStableIDs: []string{"vta_edge_b"}},
		},
	}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err != nil {
		t.Fatal(err)
	}
	var inputFactIDs string
	if err := db.db.QueryRow(`SELECT input_fact_ids FROM nodes WHERE node_type='derivation_fact' AND target_symbol_id='a'`).Scan(&inputFactIDs); err != nil {
		t.Fatal(err)
	}
	if inputFactIDs != `["vta_source_a","vta_edge_a"]` {
		t.Fatalf("input facts=%s, want candidate-bound source and edge IDs", inputFactIDs)
	}
	evidence, err := db.QueryAttachedCandidates("Run")
	if err != nil {
		t.Fatal(err)
	}
	if len(evidence) != 2 || len(evidence[0].FlowSourceStableIDs) != 1 || len(evidence[1].FlowSourceStableIDs) != 1 || evidence[0].FlowSourceStableIDs[0] == evidence[1].FlowSourceStableIDs[0] {
		t.Fatalf("normal query lost per-candidate VTA source facts: %+v", evidence)
	}
	if len(evidence[0].FlowEdgeStableIDs) != 1 || len(evidence[1].FlowEdgeStableIDs) != 1 {
		t.Fatalf("normal query lost per-candidate VTA edge facts: %+v", evidence)
	}
}

func TestVTACandidateFactsRejectUnboundIDs(t *testing.T) {
	candidate := &ResolutionCandidate{
		TargetStableID: "target", TargetNativeID: "target-native", Mechanism: "vta",
		FlowSourceStableIDs: []string{"invented-source"},
	}
	if err := validateCandidateDerivation("variable_type_flow", candidate); err == nil {
		t.Fatal("unbound VTA fact ID was accepted")
	}
}
