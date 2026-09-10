package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// RED witness for the producer nondeterminism defect AND its shipped fix's
// wrong-target defect. The class-method index behind self.method()/impl_method
// resolution was built by ranging over nodeMeta[0] (a map) with last-write-wins
// on (class, name); a class holding TWO same-named members (@overload, a
// conditional redefinition, a decorator pair) made the surviving index entry
// run-dependent, so identical trees produced different CALLS targets across
// builds. The first fix kept the lowest (start_line, id) -- deterministic, but
// it names the FIRST definition, which for an @overload group is the typing
// STUB, not the implementation (measured: 277/277 impl_method resolutions into
// click overload groups bound stubs). Python binds a name to its LAST
// definition, so the index must keep the HIGHEST (start_line, id): the
// implementation wins, deterministically.
//
// Here node 10 is the earlier def (the stub, StartLine 20) and node 11 the
// later def (the implementation, StartLine 40). Resolution must bind 11 on
// every run: first-wins (the shipped shape) fails the final assertion, and
// unordered map iteration fails the cross-run check.
//
// The same-file strategy cannot fire here: `self.dup` is qualified, and the
// ambiguous local-candidate branch only applies to unqualified calls, so
// resolution must reach Strategy 1.75 and read methodsByClass.
func TestSameNamedClassMembersResolveDeterministically(t *testing.T) {
	calls := []parser.CallRef{
		{CalleeName: "dup", CalleeQualified: "self.dup", File: "x.py", Line: 3},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", Name: "outer", File: "x.py", ParentID: 5, StartLine: 1},
		5:  {Label: "Class", Name: "Cls", File: "x.py", StartLine: 10},
		10: {Label: "Method", Name: "dup", File: "x.py", ParentID: 5, StartLine: 20},
		11: {Label: "Method", Name: "dup", File: "x.py", ParentID: 5, StartLine: 40},
	}
	nodeIDs := map[string][]int64{"outer": {1}, "Cls": {5}, "dup": {10, 11}}
	fileNodeIDs := map[string]map[string][]int64{
		"x.py": {"outer": {1}, "Cls": {5}, "dup": {10, 11}},
	}
	callerNodeIDs := []int64{1}

	const iterations = 128
	var firstTarget int64
	for run := 0; run < iterations; run++ {
		resolved, _ := resolveInternal(
			calls, nodeIDs, fileNodeIDs, callerNodeIDs, nil, nil, true, meta,
		)
		if len(resolved) != 1 {
			t.Fatalf("run %d: resolved %d calls, want exactly 1", run, len(resolved))
		}
		got := resolved[0].TargetNodeID
		if run == 0 {
			firstTarget = got
			continue
		}
		if got != firstTarget {
			t.Fatalf(
				"run %d: self.dup resolved to node %d after earlier runs chose %d — "+
					"class-method index is nondeterministic under same-name collision",
				run, got, firstTarget,
			)
		}
	}
	if firstTarget != 11 {
		t.Fatalf(
			"self.dup resolved to node %d, want 11 — last definition wins: the "+
				"later def (StartLine 40) is the implementation, the earlier "+
				"(StartLine 20) the @overload stub",
			firstTarget,
		)
	}
}
