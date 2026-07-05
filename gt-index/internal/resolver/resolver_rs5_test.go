package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// Fable RS5 (RED→GREEN): Strategy 1.95 (type_qualified) minted a FLAT 0.9 CERTIFIED whenever
// the `.`-qualifier token matched a class NAME anywhere in the repo — WITHOUT proving the
// receiver. A variable that merely shares a name with a class (a lowercase class, or an
// internal class shadowing a stdlib module — json.load / session.get) then laundered a
// CERTIFIED fact. The provenance gate (mirroring Strategy 1.9) requires same-file OR
// caller-imports-the-class-file before CERTIFIED; a `.`-qualified match without provenance
// stays CANDIDATE (0.6, evidence "type_qualified_unproven"). RED before the gate (0.9); GREEN after.
func TestResolve_RS5_DotQualifiedUnprovenReceiver_NotCertified(t *testing.T) {
	files := []string{"app/view.py", "lib/widget.py", "lib/other.py"}
	langs := []string{"python", "python", "python"}
	fm := BuildFileMap(files, langs)

	// `paint` is NON-unique (defined in TWO classes) so the unique/impl rungs skip and the
	// class-qualifier (1.95) is the rung that resolves `Widget.paint`. The caller does NOT
	// import lib/widget.py and is NOT same-dir → the receiver is UNPROVEN.
	nodeIDs := map[string][]int64{
		"view":   {1},
		"Widget": {2},
		"Other":  {4},
		"paint":  {3, 5},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"app/view.py":   {"view": {1}},
		"lib/widget.py": {"Widget": {2}, "paint": {3}},
		"lib/other.py":  {"Other": {4}, "paint": {5}},
	}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", File: "app/view.py", Name: "view"},
		2: {Label: "Class", File: "lib/widget.py", Name: "Widget"},
		3: {Label: "Method", File: "lib/widget.py", Name: "paint", ParentID: 2},
		4: {Label: "Class", File: "lib/other.py", Name: "Other"},
		5: {Label: "Method", File: "lib/other.py", Name: "paint", ParentID: 4},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "paint", CalleeQualified: "Widget.paint", Line: 5, File: "app/view.py"},
	}
	callerIDs := []int64{1}

	// No imports: the caller does NOT import lib/widget.py → receiver unproven.
	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)

	// Find the edge into Widget.paint (id 3), whichever rung resolved it.
	var got *ResolvedCall
	for i := range resolved {
		if resolved[i].TargetNodeID == 3 {
			got = &resolved[i]
			break
		}
	}
	if got == nil {
		t.Fatalf("expected an edge into Widget.paint (id 3); got %+v", resolved)
	}
	// Core RS5 assertion: a `.`-qualified, receiver-UNPROVEN call must NOT be CERTIFIED 0.9.
	if got.Confidence >= 0.9 || got.TrustTier == "CERTIFIED" {
		t.Errorf("RS5: unproven `.`-qualified receiver was CERTIFIED (method=%q conf=%.2f tier=%q ev=%q) — "+
			"a class-name match without provenance must not be a fact",
			got.Method, got.Confidence, got.TrustTier, got.EvidenceType)
	}
	if got.EvidenceType == "type_qualified" {
		t.Errorf("RS5: evidence is the CERTIFIED 'type_qualified' (conf=%.2f); the unproven path must "+
			"carry 'type_qualified_unproven'", got.Confidence)
	}
}
