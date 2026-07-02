package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ---------------------------------------------------------------------------
// B2 — methodsByClass must be built DETERMINISTICALLY.
// Go map iteration is randomized. When a class has >=2 members with the SAME name
// (conditional defs, overloads, cfg twins), the old `for id, m := range nodeMeta[0]`
// last-writer-wins picks a run-dependent winner → the resolved self.method() CALLS
// target flips between indexings (the documented ~0.5% textual/wasmi drift). The
// winner must be the stable min-(file,line,id). Mutation: reverting to the map-range
// build makes the winner flip across the N runs below (RED, reliably within N=64).
// ---------------------------------------------------------------------------
func TestResolve_B2_MethodsByClassDeterministicOnDuplicateName(t *testing.T) {
	fm := BuildFileMap([]string{"a.py"}, []string{"python"})

	// Class C(10) has TWO methods named foo: id 2 @line5 and id 3 @line9. caller(1) is
	// a method of C and calls self.foo(). min-(file,line,id) => id 2 (line 5).
	nodeIDs := map[string][]int64{"caller": {1}, "foo": {2, 3}, "C": {10}}
	fileNodeIDs := map[string]map[string][]int64{
		"a.py": {"caller": {1}, "foo": {2, 3}, "C": {10}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", Name: "caller", File: "a.py", ParentID: 10, StartLine: 2},
		10: {Label: "Class", Name: "C", File: "a.py", StartLine: 1},
		2:  {Label: "Method", Name: "foo", File: "a.py", ParentID: 10, StartLine: 5},
		3:  {Label: "Method", Name: "foo", File: "a.py", ParentID: 10, StartLine: 9},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "foo", CalleeQualified: "self.foo", Line: 3, File: "a.py"},
	}
	callerIDs := []int64{1}

	const N = 64
	var first int64 = -1
	for i := 0; i < N; i++ {
		resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
		var got int64 = -1
		for _, r := range resolved {
			if r.SourceNodeID == 1 && (r.TargetNodeID == 2 || r.TargetNodeID == 3) {
				got = r.TargetNodeID
			}
		}
		if got == -1 {
			t.Fatalf("run %d: self.foo() did not resolve to either foo method (2 or 3); resolved=%+v", i, resolved)
		}
		if first == -1 {
			first = got
		} else if got != first {
			t.Fatalf("run %d: self.foo() target flipped between runs (%d vs %d) — methodsByClass build is nondeterministic", i, got, first)
		}
	}
	if first != 2 {
		t.Errorf("self.foo() resolved to id %d; want the stable min-(file,line,id) winner id 2 (a.py:5)", first)
	}
}
