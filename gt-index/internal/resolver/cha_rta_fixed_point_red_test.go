package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// RED boundary for the next HAR-42 step-4 repair. RTA must discover an
// allocation in a reachable helper body outside the callsite's source file;
// the current same-file assignment scan intentionally fails this case.
func TestCHAThenRTAReachesCrossFileHelperAllocation(t *testing.T) {
	calls := []parser.CallRef{{
		CalleeName: "Run", CalleeQualified: "runner.Run", File: "main.go", Line: 20,
		DispatchForm: "interface",
	}}
	meta := map[int64]NodeMeta{
		2:  {Label: "Interface", Name: "Runner", File: "runner.go"},
		3:  {Label: "Struct", Name: "ImplA", File: "a.go"},
		11: {Label: "Method", Name: "Run", ParentID: 3, File: "a.go"},
	}
	implements := map[int64][]int64{3: {2}}
	assignments := []parser.AssignmentRef{{
		VarName: "runner", TypeName: "ImplA", Scope: "helper", File: "helper.go", Line: 8,
	}}
	result := AnalyzeCHAThenRTA(calls, meta, implements, assignments, true)
	if got := result[0].RTACandidateNodeIDs; !equalInt64s(got, []int64{11}) {
		t.Fatalf("cross-file helper allocation RTA=%v, want [11]", got)
	}
}
