package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

func TestResolveWithProvenanceRetainsAmbiguousCandidates(t *testing.T) {
	calls := []parser.CallRef{{CallerNodeIdx: 0, CalleeName: "target", File: "main.py", Line: 4}}
	legacy, traces := ResolveWithProvenance(calls,
		map[string][]int64{"target": {2, 3}},
		map[string]map[string][]int64{"main.py": {"target": {2, 3}}},
		[]int64{1}, nil, nil)
	if len(legacy) != 1 || len(traces) != 1 {
		t.Fatalf("legacy=%d traces=%d", len(legacy), len(traces))
	}
	trace := traces[0]
	if trace.DispatchState != DispatchAmbiguous || len(trace.CandidateNodeIDs) != 2 || trace.SelectedTargetNodeID != nil {
		t.Fatalf("unexpected trace: %+v", trace)
	}
	if err := trace.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestResolveWithProvenanceRetainsUniqueSelection(t *testing.T) {
	_, traces := ResolveWithProvenance(
		[]parser.CallRef{{CalleeName: "target", File: "main.py", Line: 2}},
		map[string][]int64{"target": {2}}, map[string]map[string][]int64{}, []int64{1}, nil, nil)
	if len(traces) != 1 || traces[0].DispatchState != DispatchUnique || traces[0].SelectedTargetNodeID == nil {
		t.Fatalf("unexpected trace: %+v", traces)
	}
	if err := traces[0].Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestResolutionCallsiteValidationRejectsForeignSelection(t *testing.T) {
	selected := int64(9)
	call := ResolutionCallsite{DispatchState: DispatchUnique, CandidateNodeIDs: []int64{2}, SelectedTargetNodeID: &selected}
	if err := call.Validate(); err == nil {
		t.Fatal("expected foreign selection rejection")
	}
}

func TestResolveWithProvenancePreservesRepeatedAndUnresolvedOrdinals(t *testing.T) {
	calls := []parser.CallRef{
		{CalleeName: "target", File: "main.py", Line: 7},
		{CalleeName: "target", File: "main.py", Line: 7},
		{CalleeName: "missing", File: "main.py", Line: 7},
	}
	legacy, traces := ResolveWithProvenance(calls,
		map[string][]int64{"target": {2}},
		map[string]map[string][]int64{"main.py": {"target": {2}}},
		[]int64{1, 1, 1}, nil, nil)
	if len(legacy) != 2 || len(traces) != len(calls) {
		t.Fatalf("legacy=%d traces=%d", len(legacy), len(traces))
	}
	if traces[0].CallsiteOrdinal != 0 || traces[1].CallsiteOrdinal != 1 {
		t.Fatalf("repeated call ordinals lost: %+v %+v", traces[0], traces[1])
	}
	if traces[0].DispatchState != DispatchUnique || traces[1].DispatchState != DispatchUnique {
		t.Fatalf("repeated calls not independently resolved: %+v %+v", traces[0], traces[1])
	}
	if traces[2].CallsiteOrdinal != 2 || traces[2].DispatchState != DispatchZero {
		t.Fatalf("unresolved same-line call misbound: %+v", traces[2])
	}
}
