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

func TestCHAThenRTAReachabilityDoesNotInventRootsOrExpandDeadCalls(t *testing.T) {
	calls := []parser.CallRef{
		{CalleeName: "helper", CalleeQualified: "helper", File: "main.go", Line: 4, DispatchForm: "static"},
		{CalleeName: "Run", CalleeQualified: "runner.Run", File: "helper.go", Line: 12, DispatchForm: "interface"},
		{CalleeName: "Run", CalleeQualified: "runner.Run", File: "dead.go", Line: 12, DispatchForm: "interface"},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "main", File: "main.go"},
		3:  {Label: "Interface", Name: "Runner", File: "runner.go"},
		4:  {Label: "Struct", Name: "ImplA", File: "a.go"},
		5:  {Label: "Struct", Name: "ImplB", File: "b.go"},
		6:  {Label: "Function", Name: "dead", File: "dead.go"},
		7:  {Label: "Function", Name: "helper", File: "helper.go"},
		10: {Label: "Method", Name: "Run", ParentID: 4, File: "a.go"},
		11: {Label: "Method", Name: "Run", ParentID: 5, File: "b.go"},
	}
	results := AnalyzeCHAThenRTAWithReachability(
		calls, meta, map[int64][]int64{4: {3}, 5: {3}}, []parser.AssignmentRef{
			{VarName: "runner", TypeName: "ImplA", Scope: "helper", File: "helper.go", Line: 8},
			{VarName: "other", TypeName: "ImplB", Scope: "dead", File: "dead.go", Line: 8},
		},
		nil, []int64{1, 7, 6}, [][]int64{{7}, nil, nil}, true,
	)
	if got := results[1].RTACandidateNodeIDs; !equalInt64s(got, []int64{10}) {
		t.Fatalf("dead caller changed reachable RTA=%v, want [10]", got)
	}
	if got := results[1].RTAAllocationTypeIDs; !equalInt64s(got, []int64{4}) {
		t.Fatalf("dead allocation leaked into RTA=%v, want [4]", got)
	}
	if got := results[1].ReachableNodeIDs; !equalInt64s(got, []int64{1, 7, 10}) {
		t.Fatalf("invented/dead roots reachable=%v, want [1 7 10]", got)
	}
	if got := results[1].RootNodeIDs; !equalInt64s(got, []int64{1}) {
		t.Fatalf("root policy=%v, want [1]", got)
	}

	unknown := AnalyzeCHAThenRTAWithReachabilityAndRoots(
		calls[1:2], meta, map[int64][]int64{4: {3}, 5: {3}}, nil,
		nil, []int64{6}, nil, nil, true,
	)
	if unknown[0].RTACompleteness != "partial" || unknown[0].RTAAbstentionReason != "roots_unknown" {
		t.Fatalf("unknown-root state=%+v, want partial/roots_unknown", unknown[0])
	}
}

func TestCHAThenRTAReachabilityHonorsMultipleRootsAndDisconnectedCycles(t *testing.T) {
	calls := []parser.CallRef{
		{CalleeName: "helperA", CalleeQualified: "helperA", DispatchForm: "static"},
		{CalleeName: "helperB", CalleeQualified: "helperB", DispatchForm: "static"},
		{CalleeName: "Run", CalleeQualified: "runner.Run", DispatchForm: "interface"},
		{CalleeName: "cycleB", CalleeQualified: "cycleB", DispatchForm: "static"},
		{CalleeName: "cycleA", CalleeQualified: "cycleA", DispatchForm: "static"},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "main", File: "main.go"},
		2:  {Label: "Function", Name: "__main__", File: "alt.go"},
		3:  {Label: "Interface", Name: "Runner", File: "runner.go"},
		4:  {Label: "Struct", Name: "ImplA", File: "a.go"},
		5:  {Label: "Struct", Name: "ImplB", File: "b.go"},
		7:  {Label: "Function", Name: "helperA", File: "helper.go"},
		8:  {Label: "Function", Name: "helperB", File: "helper2.go"},
		20: {Label: "Function", Name: "cycleA", File: "cycle.go"},
		21: {Label: "Function", Name: "cycleB", File: "cycle.go"},
		10: {Label: "Method", Name: "Run", ParentID: 4, File: "a.go"},
		11: {Label: "Method", Name: "Run", ParentID: 5, File: "b.go"},
	}
	results := AnalyzeCHAThenRTAWithReachability(
		calls, meta, map[int64][]int64{4: {3}, 5: {3}}, []parser.AssignmentRef{
			{VarName: "runner", TypeName: "ImplA", Scope: "helperA", File: "helper.go"},
			{VarName: "runner", TypeName: "ImplB", Scope: "cycleA", File: "cycle.go"},
		},
		map[int]string{2: "Runner"},
		[]int64{1, 2, 7, 20, 21},
		[][]int64{{7}, {8}, nil, {21}, {20}},
		true,
	)
	if got := EntryPointNodeIDs(meta); !equalInt64s(got, []int64{1, 2}) {
		t.Fatalf("entry roots=%v, want [1 2]", got)
	}
	if got := results[2].RTACandidateNodeIDs; !equalInt64s(got, []int64{10}) {
		t.Fatalf("reachable-root RTA=%v, want [10]", got)
	}
	if got := results[2].ReachableNodeIDs; !equalInt64s(got, []int64{1, 2, 7, 8, 10}) {
		t.Fatalf("reachable nodes=%v, want [1 2 7 8 10]", got)
	}
	if got := results[2].RootNodeIDs; !equalInt64s(got, []int64{1, 2}) {
		t.Fatalf("retained roots=%v, want [1 2]", got)
	}
	if got := results[2].RTAAllocationTypeIDs; !equalInt64s(got, []int64{4}) {
		t.Fatalf("disconnected-cycle allocation leaked=%v, want [4]", got)
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
