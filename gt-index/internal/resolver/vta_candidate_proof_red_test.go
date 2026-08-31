package resolver

import (
	"reflect"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

func TestVTAPreservesCandidateSpecificProofPaths(t *testing.T) {
	meta := map[int64]NodeMeta{
		1: {Label: "Interface", Name: "Runner"},
		2: {Label: "Struct", Name: "ImplA"},
		3: {Label: "Struct", Name: "ImplB"},
		4: {Label: "Method", Name: "Run", ParentID: 2},
		5: {Label: "Method", Name: "Run", ParentID: 3},
	}
	assignA := parser.AssignmentRef{VarName: "runner", TypeName: "ImplA", Scope: "main", File: "main.go", Line: 8}
	assignB := parser.AssignmentRef{VarName: "runner", TypeName: "ImplB", Scope: "main", File: "main.go", Line: 9}
	results := AnalyzeVTA(
		[]parser.CallRef{{CallerScope: "main", CalleeName: "Run", CalleeQualified: "runner.Run", File: "main.go", Line: 20, DispatchForm: "interface"}},
		meta,
		map[int64][]int64{2: {1}, 3: {1}},
		[]parser.AssignmentRef{assignA, assignB},
	)
	if len(results) != 1 || !reflect.DeepEqual(results[0].CandidateNodeIDs, []int64{4, 5}) {
		t.Fatalf("candidate set=%+v, want both viable methods", results)
	}
	proofs := make(map[int64]VTAFlowProof, len(results[0].FlowProofs))
	for _, proof := range results[0].FlowProofs {
		proofs[proof.CandidateNodeID] = proof
	}
	if !reflect.DeepEqual(proofs[4].SourceStableIDs, []string{vtaAssignmentStableID(assignA)}) || !reflect.DeepEqual(proofs[5].SourceStableIDs, []string{vtaAssignmentStableID(assignB)}) {
		t.Fatalf("candidate proof paths=%+v, want one assignment source per target", proofs)
	}
}

func TestVTAInterproceduralProofEdgesAreTargetKeyed(t *testing.T) {
	meta := map[int64]NodeMeta{
		1: {Label: "Interface", Name: "Runner"},
		2: {Label: "Struct", Name: "ImplA"},
		3: {Label: "Struct", Name: "ImplB"},
		4: {Label: "Method", Name: "Run", ParentID: 2, File: "a.go", ReceiverName: "rA"},
		5: {Label: "Method", Name: "Run", ParentID: 3, File: "b.go", ReceiverName: "rB"},
	}
	call := parser.CallRef{
		CallerScope: "main", CalleeName: "Run", CalleeQualified: "runner.Run",
		File: "main.go", Line: 20, DispatchForm: "interface", ArgumentNames: []string{"arg"},
	}
	assignments := []parser.AssignmentRef{
		{VarName: "runner", TypeName: "ImplA", Scope: "main", File: "main.go", Line: 8},
		{VarName: "runner", TypeName: "ImplB", Scope: "main", File: "main.go", Line: 9},
		{VarName: "arg", TypeName: "Payload", Scope: "main", File: "main.go", Line: 10},
		{VarName: "input", TypeName: "Payload", Scope: "ImplA.Run", File: "a.go", Line: 1, IsParameter: true, ParameterIndex: 0},
		{VarName: "input", TypeName: "Payload", Scope: "ImplB.Run", File: "b.go", Line: 1, IsParameter: true, ParameterIndex: 0},
	}
	results := AnalyzeVTA([]parser.CallRef{call}, meta, map[int64][]int64{2: {1}, 3: {1}}, assignments)
	if len(results) != 1 || !reflect.DeepEqual(results[0].CandidateNodeIDs, []int64{4, 5}) {
		t.Fatalf("candidate set=%+v, want both viable methods", results)
	}
	proofs := make(map[int64]VTAFlowProof, len(results[0].FlowProofs))
	for _, proof := range results[0].FlowProofs {
		proofs[proof.CandidateNodeID] = proof
	}
	for _, target := range []struct {
		id     int64
		recv   string
		formal parser.AssignmentRef
	}{
		{id: 4, recv: "rA", formal: assignments[3]},
		{id: 5, recv: "rB", formal: assignments[4]},
	} {
		proof, ok := proofs[target.id]
		if !ok {
			t.Fatalf("missing proof for candidate %d: %+v", target.id, proofs)
		}
		receiverEdge := vtaReceiverToThisStableID(call, "runner", target.id, target.recv)
		argumentEdge := vtaArgumentToFormalStableID(call, "arg", 0, target.id, &target.formal)
		if !containsVTAString(proof.EdgeStableIDs, receiverEdge) {
			t.Errorf("candidate %d proof lacks receiver-to-this edge %q: %+v", target.id, receiverEdge, proof.EdgeStableIDs)
		}
		if !containsVTAString(proof.EdgeStableIDs, argumentEdge) {
			t.Errorf("candidate %d proof lacks argument-to-formal edge %q (candidate 4=%q candidate 5=%q receivers=%q/%q): %+v", target.id, argumentEdge, vtaArgumentToFormalStableID(call, "arg", 0, 4, &assignments[3]), vtaArgumentToFormalStableID(call, "arg", 0, 5, &assignments[4]), vtaReceiverToThisStableID(call, "runner", 4, "rA"), vtaReceiverToThisStableID(call, "runner", 5, "rB"), proof.EdgeStableIDs)
		}
	}
	if containsVTAString(proofs[4].EdgeStableIDs, vtaReceiverToThisStableID(call, "runner", 5, "rB")) || containsVTAString(proofs[5].EdgeStableIDs, vtaReceiverToThisStableID(call, "runner", 4, "rA")) {
		t.Fatalf("receiver proof edge leaked across candidates: A=%v B=%v", proofs[4].EdgeStableIDs, proofs[5].EdgeStableIDs)
	}
}

// TestVTAReturnToResultAndBodyConvergenceAreCandidateBound is the order-2.5
// RED witness.  Return-type inference already makes the candidate visible, but
// before this stage the persisted proof cannot show how a factory result became
// the receiver, nor that the closed flow fixed point covered the candidate body.
func TestVTAReturnToResultAndBodyConvergenceAreCandidateBound(t *testing.T) {
	meta := map[int64]NodeMeta{
		1: {Label: "Interface", Name: "Runner"},
		2: {Label: "Struct", Name: "ImplA"},
		3: {Label: "Function", Name: "newRunner", ReturnType: "Runner"},
		4: {Label: "Method", Name: "Run", ParentID: 2, File: "a.go", ReceiverName: "rA"},
	}
	call := parser.CallRef{
		CallerScope: "main", CalleeName: "Run", CalleeQualified: "runner.Run",
		File: "main.go", Line: 20, DispatchForm: "interface", FlowAnalysisComplete: true,
	}
	resultAssignment := parser.AssignmentRef{
		VarName: "runner", TypeName: "newRunner", Scope: "main", File: "main.go", Line: 8, ViaReturn: true,
	}
	results := AnalyzeVTA([]parser.CallRef{call}, meta, map[int64][]int64{2: {1}}, []parser.AssignmentRef{resultAssignment})
	if len(results) != 1 || !reflect.DeepEqual(results[0].CandidateNodeIDs, []int64{4}) {
		t.Fatalf("return-derived candidate set=%+v, want candidate 4", results)
	}
	if len(results[0].FlowProofs) != 1 {
		t.Fatalf("return-derived proof set=%+v, want one proof", results[0].FlowProofs)
	}
	proof := results[0].FlowProofs[0]
	returnEdge := vtaStableFactID("edge", "return_to_result", resultAssignment.File, resultAssignment.Scope,
		resultAssignment.VarName, resultAssignment.TypeName, "8", "4")
	if !containsVTAString(proof.EdgeStableIDs, returnEdge) {
		t.Fatalf("candidate proof lacks return-to-result edge %q: %+v", returnEdge, proof.EdgeStableIDs)
	}
	bodyEdge := vtaStableFactID("edge", "body_convergence", call.File, call.CallerScope, "20", "Run", "4")
	if !containsVTAString(proof.EdgeStableIDs, bodyEdge) {
		t.Fatalf("closed candidate proof lacks body-convergence edge %q: %+v", bodyEdge, proof.EdgeStableIDs)
	}
}

func containsVTAString(values []string, wanted string) bool {
	for _, value := range values {
		if value == wanted {
			return true
		}
	}
	return false
}

// TestVTAFieldInsensitivePointsToUnifiesNamedFields is the order-2.6 RED
// witness. Foundational VTA uses one representative cell for a field name;
// before the field-insensitive producer stage, AnalyzeVTA incorrectly keeps
// object-scoped field facts separate and produces no candidates here.
func TestVTAFieldInsensitivePointsToUnifiesNamedFields(t *testing.T) {
	meta := map[int64]NodeMeta{
		1: {Label: "Interface", Name: "Client", File: "client.go"},
		2: {Label: "Struct", Name: "ImplA", File: "a.go"},
		3: {Label: "Struct", Name: "ImplB", File: "b.go"},
		4: {Label: "Method", Name: "Run", ParentID: 2, File: "a.go"},
		5: {Label: "Method", Name: "Run", ParentID: 3, File: "b.go"},
	}
	call := parser.CallRef{
		CallerScope: "Service.run", CalleeName: "Run", CalleeQualified: "self.client.Run",
		File: "run.go", Line: 30, DispatchForm: "interface",
	}
	results := AnalyzeVTA(
		[]parser.CallRef{call},
		meta,
		map[int64][]int64{2: {1}, 3: {1}},
		[]parser.AssignmentRef{
			{VarName: "self.client", TypeName: "ImplA", Scope: "ServiceA.init", ObjectScope: "ServiceA", File: "a_init.go", Line: 5},
			{VarName: "self.client", TypeName: "ImplB", Scope: "ServiceB.init", ObjectScope: "ServiceB", File: "b_init.go", Line: 5},
		},
	)
	assertVTAField(t, results, 0, "CandidateNodeIDs", "[4 5]")
}
