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
