package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// TestVTAFieldSensitivePointsToSeparatesNamedFieldsByObject is the HAR-42
// order-2.7 RED witness. The field-insensitive representative cell from the
// preceding stage must not leak a value written on ServiceB into ServiceA.
func TestVTAFieldSensitivePointsToSeparatesNamedFieldsByObject(t *testing.T) {
	meta := map[int64]NodeMeta{
		1: {Label: "Interface", Name: "Client", File: "client.go"},
		2: {Label: "Struct", Name: "ImplA", File: "a.go"},
		3: {Label: "Struct", Name: "ImplB", File: "b.go"},
		4: {Label: "Method", Name: "Run", ParentID: 2, File: "a.go"},
		5: {Label: "Method", Name: "Run", ParentID: 3, File: "b.go"},
	}
	implements := map[int64][]int64{2: {1}, 3: {1}}
	assignments := []parser.AssignmentRef{
		{VarName: "self.client", TypeName: "ImplA", Scope: "ServiceA.init", ObjectScope: "ServiceA", File: "a_init.go", Line: 5},
		{VarName: "self.client", TypeName: "ImplB", Scope: "ServiceB.init", ObjectScope: "ServiceB", File: "b_init.go", Line: 5},
	}
	calls := []parser.CallRef{
		{CallerScope: "ServiceA.run", CalleeName: "Run", CalleeQualified: "self.client.Run", File: "run.go", Line: 30, DispatchForm: "interface"},
		{CallerScope: "ServiceB.run", CalleeName: "Run", CalleeQualified: "self.client.Run", File: "run.go", Line: 30, DispatchForm: "interface"},
	}

	results := AnalyzeVTA(calls, meta, implements, assignments)
	if len(results) != 2 {
		t.Fatalf("got %d VTA results, want 2: %+v", len(results), results)
	}
	if got, want := results[0].CandidateNodeIDs, []int64{4}; !equalIDs(got, want) {
		t.Fatalf("ServiceA field points-to=%v, want %v", got, want)
	}
	if got, want := results[1].CandidateNodeIDs, []int64{5}; !equalIDs(got, want) {
		t.Fatalf("ServiceB field points-to=%v, want %v", got, want)
	}
}

func equalIDs(got, want []int64) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}
