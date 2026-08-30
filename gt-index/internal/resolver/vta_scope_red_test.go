package resolver

import (
	"reflect"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

func TestVTASameFileScopeBoundary(t *testing.T) {
	meta := map[int64]NodeMeta{
		1: {Label: "Interface", Name: "Runner"},
		2: {Label: "Struct", Name: "ImplA"},
		3: {Label: "Struct", Name: "ImplB"},
		4: {Label: "Method", Name: "Run", ParentID: 2},
		5: {Label: "Method", Name: "Run", ParentID: 3},
	}
	got := AnalyzeVTA(
		[]parser.CallRef{{CallerScope: "first", CalleeName: "Run", CalleeQualified: "runner.Run", File: "same.go", Line: 30, DispatchForm: "interface"}},
		meta,
		map[int64][]int64{2: {1}, 3: {1}},
		[]parser.AssignmentRef{
			{VarName: "runner", TypeName: "ImplA", Scope: "first", File: "same.go", Line: 5},
			{VarName: "runner", TypeName: "ImplB", Scope: "second", File: "same.go", Line: 6},
		},
	)
	if len(got) != 1 || !reflect.DeepEqual(got[0].CandidateNodeIDs, []int64{4}) {
		t.Fatalf("same-file scopes merged: %#v", got)
	}
}
