package resolver

import (
	"reflect"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

func TestResolveWithProvenanceConservesUniqueAndAmbiguousCandidates(t *testing.T) {
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "unique", Line: 10, File: "caller.py"},
		{CallerNodeIdx: 0, CalleeName: "ambiguous", Line: 20, File: "caller.py"},
	}
	nodeIDs := map[string][]int64{
		"unique":    {2},
		"ambiguous": {3, 4},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"caller.py": {"unique": {2}, "ambiguous": {3, 4}},
	}

	edges, callsites := ResolveWithProvenance(
		calls, nodeIDs, fileNodeIDs, []int64{1, 1}, nil, nil,
	)
	if len(edges) != 2 {
		t.Fatalf("legacy selected edges = %d; want 2", len(edges))
	}
	if len(callsites) != 2 {
		t.Fatalf("callsites = %d; want 2", len(callsites))
	}
	if got := callsites[0]; got.DispatchState != DispatchUnique ||
		!reflect.DeepEqual(got.CandidateNodeIDs, []int64{2}) ||
		got.SelectedTargetNodeID == nil || *got.SelectedTargetNodeID != 2 {
		t.Fatalf("unique callsite = %#v", got)
	}
	if got := callsites[1]; got.DispatchState != DispatchAmbiguous ||
		!reflect.DeepEqual(got.CandidateNodeIDs, []int64{3, 4}) ||
		got.SelectedTargetNodeID != nil {
		t.Fatalf("ambiguous callsite = %#v", got)
	}
}

func TestResolveWithProvenanceRetainsUnresolvedCallsites(t *testing.T) {
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "missing", Line: 10, File: "caller.py"},
		{CallerNodeIdx: 0, CalleeName: "invoke", CalleeQualified: "obj.invoke", Line: 20, File: "caller.py"},
		{CallerNodeIdx: 0, CalleeName: "Get", CalleeQualified: "http.Get", Line: 30, File: "caller.py"},
		{CallerNodeIdx: 0, CalleeName: "broken", Line: 40, File: "caller.py", ParserIncomplete: true},
	}
	imports := []parser.ImportRef{{ImportedName: "http", ModulePath: "net/http", File: "caller.py"}}

	_, callsites := ResolveWithProvenance(
		calls, map[string][]int64{}, map[string]map[string][]int64{},
		[]int64{1, 1, 1, 1}, imports, map[string][]string{},
	)
	want := []DispatchState{
		DispatchZero,
		DispatchDynamic,
		DispatchExternalUnresolved,
		DispatchParserIncomplete,
	}
	if len(callsites) != len(want) {
		t.Fatalf("callsites = %d; want %d", len(callsites), len(want))
	}
	for i, state := range want {
		if callsites[i].DispatchState != state {
			t.Errorf("callsite %d state = %q; want %q", i, callsites[i].DispatchState, state)
		}
		if len(callsites[i].CandidateNodeIDs) != 0 || callsites[i].SelectedTargetNodeID != nil {
			t.Errorf("unresolved callsite %d retained authority: %#v", i, callsites[i])
		}
	}
}

func TestResolutionCallsiteValidationRejectsNonDenseOrForeignSelection(t *testing.T) {
	selected := int64(99)
	for _, record := range []ResolutionCallsite{
		{CandidateNodeIDs: []int64{2, 2}, DispatchState: DispatchAmbiguous},
		{CandidateNodeIDs: []int64{2}, SelectedTargetNodeID: &selected, DispatchState: DispatchUnique},
	} {
		if err := record.Validate(); err == nil {
			t.Fatalf("Validate accepted invalid record: %#v", record)
		}
	}
}
