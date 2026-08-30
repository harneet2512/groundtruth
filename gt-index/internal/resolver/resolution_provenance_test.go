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

func TestResolveWithProvenanceKeepsGlobalUniqueHeuristicUnselected(t *testing.T) {
	_, traces := ResolveWithProvenance(
		[]parser.CallRef{{CalleeName: "target", File: "main.py", Line: 2}},
		map[string][]int64{"target": {2}}, map[string]map[string][]int64{}, []int64{1}, nil, nil)
	if len(traces) != 1 || traces[0].DispatchState != DispatchCandidateOnly || traces[0].SelectedTargetNodeID != nil {
		t.Fatalf("unexpected trace: %+v", traces)
	}
	if err := traces[0].Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestResolveWithProvenanceSelectsClosedSourceEvidence(t *testing.T) {
	_, traces := ResolveWithProvenance(
		[]parser.CallRef{{CalleeName: "target", File: "main.py", Line: 2}},
		map[string][]int64{"target": {2}}, map[string]map[string][]int64{"main.py": {"target": {2}}}, []int64{1}, nil, nil)
	if len(traces) != 1 || traces[0].DispatchState != DispatchUnique || traces[0].SelectedTargetNodeID == nil || traces[0].VerificationStatus != "source_supported" {
		t.Fatalf("source-supported same-file resolution was not selected: %+v", traces)
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
		{CalleeName: "target", File: "main.py", Line: 7},
	}
	legacy, traces := ResolveWithProvenance(calls,
		map[string][]int64{"target": {2}},
		map[string]map[string][]int64{"main.py": {"target": {2}}},
		[]int64{1, 1, 1, 1}, nil, nil)
	if len(legacy) != 1 || len(traces) != len(calls) {
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
	if traces[3].CallsiteOrdinal != 3 || traces[3].DispatchState != DispatchUnique {
		t.Fatalf("resolved call after same-line unresolved call was lost: %+v", traces[3])
	}
}

func TestResolveWithProvenanceImplMethodRetainsAllViableTargets(t *testing.T) {
	files := []string{"caller.py", "other.py"}
	fm := BuildFileMap(files, []string{"python", "python"})
	nodeIDs := map[string][]int64{
		"run": {4, 7},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"caller.py": {},
		"other.py":  {"run": {4, 7}},
	}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", File: "caller.py", Name: "caller"},
		3: {Label: "Class", File: "caller.py", Name: "First"},
		4: {Label: "Method", File: "caller.py", Name: "run", ParentID: 3},
		6: {Label: "Class", File: "other.py", Name: "Second"},
		7: {Label: "Method", File: "other.py", Name: "run", ParentID: 6},
	}
	calls := []parser.CallRef{{CallerNodeIdx: 0, CalleeName: "run", CalleeQualified: "obj.run", File: "caller.py", Line: 8}}
	legacy, traces := ResolveWithProvenance(calls, nodeIDs, fileNodeIDs, []int64{1}, nil, fm, meta)
	if len(legacy) != 1 || len(traces) != 1 {
		t.Fatalf("legacy=%d traces=%d", len(legacy), len(traces))
	}
	if got := traces[0].CandidateNodeIDs; len(got) != 2 || got[0] != 4 || got[1] != 7 {
		t.Fatalf("impl_method candidates=%v, want [4 7]", got)
	}
	if traces[0].Mechanism != "impl_method" || len(traces[0].CandidateNodeIDs) != 2 {
		t.Fatalf("impl_method provenance=%+v, want mechanism impl_method and count 2", traces[0])
	}
	if traces[0].DispatchState != DispatchAmbiguous || traces[0].SelectedTargetNodeID != nil {
		t.Fatalf("impl_method ambiguity leaked selection: %+v", traces[0])
	}
	if err := traces[0].Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestResolveWithProvenanceAbstainsOnDynamicAndParserIncompleteCalls(t *testing.T) {
	calls := []parser.CallRef{
		{CalleeName: "handlers[name]", CalleeQualified: "handlers[name]", File: "main.js", Line: 3, DynamicDispatch: true},
		{CalleeName: "target", File: "main.js", Line: 4, ParserIncomplete: true},
	}
	legacy, traces := ResolveWithProvenance(calls,
		map[string][]int64{"handlers[name]": {2}, "target": {3}},
		map[string]map[string][]int64{"main.js": {"handlers[name]": {2}, "target": {3}}},
		[]int64{1, 1}, nil, nil)
	if len(legacy) != 0 || len(traces) != 2 {
		t.Fatalf("legacy=%d traces=%d", len(legacy), len(traces))
	}
	if traces[0].DispatchState != DispatchDynamic || traces[0].Mechanism != "dynamic" {
		t.Fatalf("dynamic call was not conservatively classified: %+v", traces[0])
	}
	if traces[1].DispatchState != DispatchParserIncomplete {
		t.Fatalf("parser-incomplete call was not preserved: %+v", traces[1])
	}
	for _, trace := range traces {
		if err := trace.Validate(); err != nil {
			t.Fatal(err)
		}
	}
}

func TestResolutionCallsiteRequiresExplicitDynamicAbstention(t *testing.T) {
	call := ResolutionCallsite{DispatchState: DispatchDynamic, CandidateNodeIDs: []int64{2, 3}}
	if err := call.Validate(); err == nil {
		t.Fatal("dynamic candidate evidence must not contradict the producer abstention policy")
	}
}

func TestSourceAuthorityDoesNotDeriveFromConfidence(t *testing.T) {
	highHeuristic := ResolvedCall{Method: "verified_unique", EvidenceType: "name_unique", Confidence: 1.0, CandidateNodeIDs: []int64{2}}
	if sourceSupportedResolution(highHeuristic) {
		t.Fatal("heuristic confidence created source authority")
	}
	lowButSourced := ResolvedCall{Method: "return_type", EvidenceType: "return_type_flow", Confidence: 0.1, CandidateNodeIDs: []int64{2}}
	if !sourceSupportedResolution(lowButSourced) {
		t.Fatal("closed source provenance was incorrectly gated by confidence")
	}
}

func TestResolutionCallsiteRejectsUnknownReceiverOrigin(t *testing.T) {
	call := ResolutionCallsite{DispatchState: DispatchZero, ReceiverOrigin: "invented"}
	if err := call.Validate(); err == nil {
		t.Fatal("unknown receiver origin accepted")
	}
}

func TestResolveWithProvenanceDeduplicatesEquivalentImportCandidates(t *testing.T) {
	calls := []parser.CallRef{{CalleeName: "target", File: "main.py", Line: 2}}
	imports := []parser.ImportRef{
		{ImportedName: "target", ModulePath: "mod", File: "main.py"},
		{ImportedName: "target", ModulePath: "mod", File: "main.py"},
	}
	_, traces := ResolveWithProvenance(calls,
		map[string][]int64{"target": {2}},
		map[string]map[string][]int64{"mod.py": {"target": {2}}},
		[]int64{1}, imports, map[string][]string{"mod": {"mod.py"}})
	if len(traces) != 1 || len(traces[0].CandidateNodeIDs) != 1 || traces[0].CandidateNodeIDs[0] != 2 {
		t.Fatalf("duplicate import identities were not coalesced: %+v", traces)
	}
	if err := traces[0].Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestResolveWithProvenanceKeepsWeakSingletonUnselected(t *testing.T) {
	calls := []parser.CallRef{{CalleeName: "run", CalleeQualified: "obj.run", File: "main.py", Line: 2}}
	legacy, traces := ResolveWithProvenance(calls,
		map[string][]int64{"run": {2}},
		map[string]map[string][]int64{"other.py": {"run": {2}}},
		[]int64{1}, nil, nil,
		map[int64]NodeMeta{1: {Label: "Function", File: "main.py"}, 2: {Label: "Function", File: "other.py"}})
	if len(legacy) != 1 || len(traces) != 1 {
		t.Fatalf("legacy=%d traces=%d", len(legacy), len(traces))
	}
	trace := traces[0]
	if trace.DispatchState != DispatchCandidateOnly || trace.SelectedTargetNodeID != nil || trace.VerificationStatus != "candidate_only" || len(trace.CandidateNodeIDs) != 1 {
		t.Fatalf("weak singleton leaked selected authority: %+v", trace)
	}
	if err := trace.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestResolveWithProvenanceBindsImportEvidenceToEachCandidate(t *testing.T) {
	calls := []parser.CallRef{{CalleeName: "target", File: "main.py", Line: 3}}
	imports := []parser.ImportRef{
		{ImportedName: "target", ModulePath: "a", File: "main.py"},
		{ImportedName: "target", ModulePath: "b", File: "main.py"},
	}
	_, traces := ResolveWithProvenance(calls,
		map[string][]int64{"target": {2, 3}},
		map[string]map[string][]int64{"a.py": {"target": {2}}, "b.py": {"target": {3}}},
		[]int64{1}, imports, map[string][]string{"a": {"a.py"}, "b": {"b.py"}},
		map[int64]NodeMeta{1: {File: "main.py"}, 2: {File: "a.py"}, 3: {File: "b.py"}})
	if len(traces) != 1 || len(traces[0].CandidateNodeIDs) != 2 {
		t.Fatalf("candidate trace=%+v", traces)
	}
	if got := traces[0].CandidateImportChains[2]; len(got) != 1 || got[0] != "a:target" {
		t.Fatalf("candidate 2 import evidence=%v", got)
	}
	if got := traces[0].CandidateImportChains[3]; len(got) != 1 || got[0] != "b:target" {
		t.Fatalf("candidate 3 import evidence=%v", got)
	}
}
