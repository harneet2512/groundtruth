package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

func TestCHAThenRTAReachabilityReachesSecondIteration(t *testing.T) {
	calls := []parser.CallRef{
		{CalleeName: "helper", CalleeQualified: "helper", File: "main.go", Line: 4, DispatchForm: "static"},
		{CalleeName: "Run", CalleeQualified: "runner.Run", File: "main.go", Line: 10, DispatchForm: "interface"},
		{CalleeName: "Run", CalleeQualified: "other.Run", File: "a.go", Line: 20, DispatchForm: "interface"},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "main", File: "main.go"},
		2:  {Label: "Function", Name: "helper", File: "helper.go"},
		3:  {Label: "Interface", Name: "Runner", File: "runner.go"},
		4:  {Label: "Struct", Name: "ImplA", File: "a.go"},
		5:  {Label: "Struct", Name: "ImplB", File: "b.go"},
		10: {Label: "Method", Name: "Run", ParentID: 4, File: "a.go"},
		11: {Label: "Method", Name: "Run", ParentID: 5, File: "b.go"},
	}
	implements := map[int64][]int64{4: {3}, 5: {3}}
	assignments := []parser.AssignmentRef{
		{VarName: "runner", TypeName: "ImplA", Scope: "helper", File: "helper.go", Line: 8},
		{VarName: "other", TypeName: "ImplB", Scope: "Run", File: "a.go", Line: 15},
	}
	results := AnalyzeCHAThenRTAWithReachability(
		calls, meta, implements, assignments,
		map[int]string{1: "Runner", 2: "Runner"},
		[]int64{1, 1, 10}, [][]int64{{2}, nil, nil}, true,
	)
	if got := results[1].RTACandidateNodeIDs; !equalInt64s(got, []int64{10, 11}) {
		t.Fatalf("fixed-point RTA candidates=%v, want [10 11]", got)
	}
	if got := results[1].RTAAllocationTypeIDs; !equalInt64s(got, []int64{4, 5}) {
		t.Fatalf("fixed-point allocations=%v, want [4 5]", got)
	}
	if got := results[1].ReachableNodeIDs; !equalInt64s(got, []int64{1, 2, 10, 11}) {
		t.Fatalf("reachable nodes=%v, want [1 2 10 11]", got)
	}
}

func TestCHAThenRTAReachabilityIgnoresUnreachableAllocation(t *testing.T) {
	calls := []parser.CallRef{{CalleeName: "Run", CalleeQualified: "runner.Run", File: "main.go", Line: 10, DispatchForm: "interface"}}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "main", File: "main.go"},
		3:  {Label: "Interface", Name: "Runner", File: "runner.go"},
		4:  {Label: "Struct", Name: "ImplA", File: "a.go"},
		5:  {Label: "Struct", Name: "ImplB", File: "b.go"},
		10: {Label: "Method", Name: "Run", ParentID: 4, File: "a.go"},
		11: {Label: "Method", Name: "Run", ParentID: 5, File: "b.go"},
	}
	results := AnalyzeCHAThenRTAWithReachability(
		calls, meta, map[int64][]int64{4: {3}, 5: {3}}, []parser.AssignmentRef{
			{VarName: "runner", TypeName: "ImplA", Scope: "main", File: "main.go", Line: 6},
			{VarName: "dead", TypeName: "ImplB", Scope: "dead", File: "dead.go", Line: 2},
		},
		map[int]string{0: "Runner"}, []int64{1}, nil, true,
	)
	if got := results[0].RTACandidateNodeIDs; !equalInt64s(got, []int64{10}) {
		t.Fatalf("unreachable allocation leaked into RTA=%v, want [10]", got)
	}
	if got := results[0].RTAAllocationTypeIDs; !equalInt64s(got, []int64{4}) {
		t.Fatalf("unreachable allocation types=%v, want [4]", got)
	}
}

func TestCHAThenRTAUsesCompleteMethodSignatures(t *testing.T) {
	calls := []parser.CallRef{{CalleeName: "Run", CalleeQualified: "runner.Run", DispatchForm: "interface"}}
	meta := map[int64]NodeMeta{
		2:  {Label: "Interface", Name: "Runner", Signature: "type Runner interface { Run(int) error }"},
		3:  {Label: "Struct", Name: "ImplA"},
		4:  {Label: "Struct", Name: "ImplUnrelated"},
		20: {Label: "Method", Name: "Run", ParentID: 2, Signature: "Run(int) error"},
		11: {Label: "Method", Name: "Run", ParentID: 3, Signature: "func (ImplA) Run(int) error"},
		12: {Label: "Method", Name: "Run", ParentID: 4, Signature: "func (ImplUnrelated) Run(int) string"},
	}
	result := AnalyzeCHAThenRTA(calls, meta, nil, nil, true)
	if got := result[0].CHACandidateNodeIDs; !equalInt64s(got, []int64{11}) {
		t.Fatalf("signature-sensitive CHA=%v, want [11]", got)
	}
}
