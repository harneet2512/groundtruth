package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

func TestVTANormalFlowRequiresClosedAnalysisBoundary(t *testing.T) {
	meta := map[int64]NodeMeta{
		1: {Label: "Interface", Name: "Runner", File: "runner.go"},
		2: {Label: "Struct", Name: "ImplA", File: "a.go"},
		3: {Label: "Method", Name: "Run", ParentID: 2, File: "a.go"},
	}
	results := AnalyzeVTA(
		[]parser.CallRef{{CallerScope: "main", CalleeName: "Run", CalleeQualified: "runner.Run", File: "main.go", Line: 10, DispatchForm: "interface"}},
		meta,
		map[int64][]int64{2: {1}},
		[]parser.AssignmentRef{{VarName: "runner", TypeName: "ImplA", Scope: "main", File: "main.go", Line: 5}},
	)
	if len(results) != 1 || results[0].Completeness != "partial" {
		t.Fatalf("parser-complete flow without an explicit closed analysis boundary=%+v, want partial", results)
	}
}
