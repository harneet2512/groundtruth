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
			{TargetID: targetA, TargetStableID: "a", TargetNativeID: "a", Ordinal: 0, Mechanism: "vta",
				FlowSourceStableIDs: []string{"vta_source_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
				FlowEdgeStableIDs:   []string{"vta_edge_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
				FlowSourceFacts:     []ResolutionFlowFact{{StableID: "vta_source_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", Kind: "source", CallsiteID: "vta-facts", TargetStableID: "a", Payload: "runner=ImplA"}},
				FlowEdgeFacts:       []ResolutionFlowFact{{StableID: "vta_edge_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", Kind: "edge", CallsiteID: "vta-facts", TargetStableID: "a", Payload: "caller->runner"}}},
			{TargetID: targetB, TargetStableID: "b", TargetNativeID: "b", Ordinal: 1, Mechanism: "vta",
				FlowSourceStableIDs: []string{"vta_source_cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},
				FlowEdgeStableIDs:   []string{"vta_edge_ddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},
				FlowSourceFacts:     []ResolutionFlowFact{{StableID: "vta_source_cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", Kind: "source", CallsiteID: "vta-facts", TargetStableID: "b", Payload: "runner=ImplB"}},
				FlowEdgeFacts:       []ResolutionFlowFact{{StableID: "vta_edge_ddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", Kind: "edge", CallsiteID: "vta-facts", TargetStableID: "b", Payload: "caller->runner"}}},
		},
	}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err != nil {
		t.Fatal(err)
	}
	var inputFactIDs string
	if err := db.db.QueryRow(`SELECT input_fact_ids FROM nodes WHERE node_type='derivation_fact' AND target_symbol_id='a'`).Scan(&inputFactIDs); err != nil {
		t.Fatal(err)
	}
	if inputFactIDs != `["vta_source_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","vta_edge_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]` {
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

func TestVTACandidateProvenancePersistsReferentialTypedFacts(t *testing.T) {
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
	otherTargetID, err := db.InsertNode(&Node{Label: "Method", Name: "Run", QualifiedName: "ImplB.Run", FilePath: "b.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	row := AttachedResolution{
		Source:     &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"},
		Callsite:   &ResolutionCallsite{CallsiteID: "vta-fact-refs", SourceID: sourceID, SourceFile: "main.go", SourceLine: 8, Callee: "Run", DispatchState: "ambiguous", DispatchForm: "interface", CandidateCount: 2, Mechanism: "vta", PassCoverage: []ResolutionPassCoverage{{PassKind: "vta", Status: "partial", Reason: "candidate_only_flow_evidence"}}},
		Candidates: []*ResolutionCandidate{{TargetID: targetID, TargetStableID: "a", TargetNativeID: "a", Ordinal: 0, Mechanism: "vta", FlowSourceStableIDs: []string{"vta_source_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}, FlowEdgeStableIDs: []string{"vta_edge_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}, FlowSourceFacts: []ResolutionFlowFact{{StableID: "vta_source_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", Kind: "source", CallsiteID: "vta-fact-refs", TargetStableID: "a", Payload: "runner=ImplA"}}, FlowEdgeFacts: []ResolutionFlowFact{{StableID: "vta_edge_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", Kind: "edge", CallsiteID: "vta-fact-refs", TargetStableID: "a", Payload: "caller->runner"}}}, {TargetID: otherTargetID, TargetStableID: "b", TargetNativeID: "b", Ordinal: 1, Mechanism: "vta", FlowSourceStableIDs: []string{"vta_source_cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}, FlowEdgeStableIDs: []string{"vta_edge_ddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}, FlowSourceFacts: []ResolutionFlowFact{{StableID: "vta_source_cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", Kind: "source", CallsiteID: "vta-fact-refs", TargetStableID: "b", Payload: "runner=ImplB"}}, FlowEdgeFacts: []ResolutionFlowFact{{StableID: "vta_edge_ddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", Kind: "edge", CallsiteID: "vta-fact-refs", TargetStableID: "b", Payload: "caller->runner"}}}},
	}
	if err := db.AttachResolutionGraph(testGraphIdentity("refs"), []AttachedResolution{row}); err != nil {
		t.Fatal(err)
	}
	for _, fact := range []struct {
		id       string
		typeName string
	}{
		{id: "vta_source_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", typeName: "vta_flow_source_fact"},
		{id: "vta_edge_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", typeName: "vta_flow_edge_fact"},
	} {
		var nodeType, callsiteID string
		if err := db.db.QueryRow(`SELECT node_type, COALESCE(callsite_id,'') FROM nodes WHERE stable_id=?`, fact.id).Scan(&nodeType, &callsiteID); err != nil {
			t.Fatalf("typed VTA fact %s was not persisted: %v", fact.id, err)
		}
		if nodeType != fact.typeName || callsiteID != row.Callsite.CallsiteID {
			t.Fatalf("typed VTA fact %s=(type %q, callsite %q), want (%q, %q)", fact.id, nodeType, callsiteID, fact.typeName, row.Callsite.CallsiteID)
		}
	}
}

func TestVTACandidateProvenanceRejectsUnknownPrefixedFact(t *testing.T) {
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
	row := AttachedResolution{
		Source:     &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"},
		Callsite:   &ResolutionCallsite{CallsiteID: "vta-fact-unknown", SourceID: sourceID, SourceFile: "main.go", SourceLine: 8, Callee: "Run", DispatchState: "unique", DispatchForm: "interface", CandidateCount: 1, Mechanism: "vta"},
		Candidates: []*ResolutionCandidate{{TargetID: targetID, TargetStableID: "a", TargetNativeID: "a", Ordinal: 0, Mechanism: "vta", FlowSourceStableIDs: []string{"vta_source_ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}}},
	}
	if err := db.AttachResolutionGraph(testGraphIdentity("unknown"), []AttachedResolution{row}); err == nil {
		t.Fatal("unknown but prefix-shaped VTA fact was accepted")
	}
}
