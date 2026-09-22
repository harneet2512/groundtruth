package resolver

import (
	"reflect"
	"sort"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// TestVTAProofIDsAreRowidInvariant: VTA fact ids are published as stable_id
// (and hashed into vta_flow_edge_fact node names). They must not change when
// the same symbols carry different rowids — a batch amend re-inserts changed
// rows at the top of the id space, a clean rebuild numbers them afresh, and
// the convergence harness saw the same proof published under two ids.
func TestVTAProofIDsAreRowidInvariant(t *testing.T) {
	proofIDs := func(runA, runB, implA, implB int64) []string {
		meta := map[int64]NodeMeta{
			1:     {Label: "Interface", Name: "Runner"},
			implA: {Label: "Struct", Name: "ImplA", File: "a.go", StartLine: 3},
			implB: {Label: "Struct", Name: "ImplB", File: "b.go", StartLine: 3},
			runA:  {Label: "Method", Name: "Run", ParentID: implA, File: "a.go", ReceiverName: "rA", StartLine: 5},
			runB:  {Label: "Method", Name: "Run", ParentID: implB, File: "b.go", ReceiverName: "rB", StartLine: 5},
		}
		call := parser.CallRef{CallerScope: "main", CalleeName: "Run", CalleeQualified: "runner.Run",
			File: "main.go", Line: 20, DispatchForm: "interface", FlowAnalysisComplete: true, ArgumentNames: []string{"arg"}}
		assignments := []parser.AssignmentRef{
			{VarName: "runner", TypeName: "ImplA", Scope: "main", File: "main.go", Line: 8},
			{VarName: "runner", TypeName: "ImplB", Scope: "main", File: "main.go", Line: 9},
			{VarName: "arg", TypeName: "Payload", Scope: "main", File: "main.go", Line: 10},
		}
		results := AnalyzeVTA([]parser.CallRef{call}, meta, map[int64][]int64{implA: {1}, implB: {1}}, assignments)
		var out []string
		for _, r := range results {
			for _, p := range r.FlowProofs {
				out = append(out, p.EdgeStableIDs...)
			}
		}
		sort.Strings(out)
		return out
	}
	parent := proofIDs(4, 5, 2, 3)
	amended := proofIDs(95, 94, 2, 3)
	if len(parent) == 0 {
		t.Fatal("fixture produced no VTA proof edges")
	}
	if !reflect.DeepEqual(parent, amended) {
		t.Fatalf("VTA proof edge ids depend on rowids:\n parent: %v\n amended: %v", parent, amended)
	}
}
