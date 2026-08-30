package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// RED boundary for HAR-42 step 4. The accepted producer currently has no
// hierarchy-analysis API, so this test must fail before implementation. The
// fixture is deliberately small but exercises the required state transitions:
// closed-world CHA breadth, RTA narrowing after an allocation, and open-world
// abstention without collapsing the conservative CHA set.
func TestCHAThenRTAClosedAndOpenBoundaries(t *testing.T) {
	calls := []parser.CallRef{{
		CalleeName: "Run", CalleeQualified: "runner.Run", File: "main.go", Line: 8,
		DispatchForm: "interface",
	}}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "main", File: "main.go"},
		2:  {Label: "Interface", Name: "Runner", File: "runner.go"},
		3:  {Label: "Struct", Name: "ImplA", File: "a.go"},
		4:  {Label: "Struct", Name: "ImplB", File: "b.go"},
		11: {Label: "Method", Name: "Run", ParentID: 3, File: "a.go"},
		12: {Label: "Method", Name: "Run", ParentID: 4, File: "b.go"},
	}
	implements := map[int64][]int64{3: {2}, 4: {2}}

	closed := AnalyzeCHAThenRTA(calls, meta, implements, nil, true)
	if len(closed) != 1 {
		t.Fatalf("closed result count=%d, want 1", len(closed))
	}
	if got := closed[0].CHACandidateNodeIDs; !equalInt64s(got, []int64{11, 12}) {
		t.Fatalf("CHA candidates=%v, want [11 12]", got)
	}
	if got := closed[0].RTACandidateNodeIDs; len(got) != 0 {
		t.Fatalf("RTA without allocation=%v, want empty", got)
	}
	if closed[0].RTACompleteness != "closed" {
		t.Fatalf("RTA completeness=%q, want closed", closed[0].RTACompleteness)
	}

	allocated := AnalyzeCHAThenRTA(calls, meta, implements, []parser.AssignmentRef{{
		VarName: "runner", TypeName: "ImplA", Scope: "main", File: "main.go", Line: 6,
	}}, true)
	if got := allocated[0].RTACandidateNodeIDs; !equalInt64s(got, []int64{11}) {
		t.Fatalf("RTA candidates=%v, want [11] after allocation", got)
	}
	if got := allocated[0].RTAAllocationTypeIDs; !equalInt64s(got, []int64{3}) {
		t.Fatalf("RTA allocations=%v, want [3]", got)
	}

	open := AnalyzeCHAThenRTA(calls, meta, implements, []parser.AssignmentRef{{
		VarName: "runner", TypeName: "ImplA", Scope: "main", File: "main.go", Line: 6,
	}}, false)
	if got := open[0].CHACandidateNodeIDs; !equalInt64s(got, []int64{11, 12}) {
		t.Fatalf("open CHA candidates=%v, want [11 12]", got)
	}
	if open[0].RTACompleteness != "partial" || open[0].RTAAbstentionReason != "hierarchy_open" {
		t.Fatalf("open RTA state=%+v, want partial/hierarchy_open", open[0])
	}
}

func equalInt64s(got, want []int64) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range want {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}

func TestCHAThenRTAUsesDeclaredReceiverBoundaryAndTransitiveImplementors(t *testing.T) {
	calls := []parser.CallRef{{CalleeName: "Run", CalleeQualified: "runner.Run", File: "main.go", Line: 20, DispatchForm: "interface"}}
	meta := map[int64]NodeMeta{
		2:  {Label: "Interface", Name: "Runner", File: "runner.go"},
		3:  {Label: "Interface", Name: "Unrelated", File: "unrelated.go"},
		4:  {Label: "Struct", Name: "ImplA", File: "a.go"},
		5:  {Label: "Struct", Name: "ImplUnrelated", File: "u.go"},
		11: {Label: "Method", Name: "Run", ParentID: 4, File: "a.go"},
		12: {Label: "Method", Name: "Run", ParentID: 5, File: "u.go"},
		20: {Label: "Struct", Name: "Base", File: "base.go"},
		21: {Label: "Struct", Name: "Derived", File: "derived.go"},
		22: {Label: "Method", Name: "Run", ParentID: 21, File: "derived.go"},
	}
	implements := map[int64][]int64{4: {2}, 5: {3}, 21: {20}, 20: {2}}
	assignments := []parser.AssignmentRef{{VarName: "runner", TypeName: "Runner", Scope: "main", File: "main.go", Line: 6}}
	result := AnalyzeCHAThenRTA(calls, meta, implements, assignments, true)
	if got := result[0].CHACandidateNodeIDs; !equalInt64s(got, []int64{11, 22}) {
		t.Fatalf("declared Runner CHA=%v, want Runner methods [11 22]", got)
	}
	if got := result[0].RTACandidateNodeIDs; len(got) != 0 {
		t.Fatalf("unallocated Runner RTA=%v, want empty", got)
	}

	transitiveMeta := map[int64]NodeMeta{
		2:  {Label: "Interface", Name: "Runner"},
		20: {Label: "Struct", Name: "Base"},
		21: {Label: "Struct", Name: "Derived"},
		22: {Label: "Method", Name: "Run", ParentID: 21},
	}
	transitive := AnalyzeCHAThenRTA(calls, transitiveMeta, map[int64][]int64{20: {2}, 21: {20}}, assignments, true)
	if got := transitive[0].CHACandidateNodeIDs; !equalInt64s(got, []int64{22}) {
		t.Fatalf("transitive CHA=%v, want [22]", got)
	}
}
