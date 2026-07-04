package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ----------------------------------------------------------------------------
// E (edge-truth): the MULTI-candidate qualified-unresolved last-chance branch.
//
// LAYERING (why this fires only on the >3-implementor residual): the receiver-
// unproven impl_method rung (1.94, resolver.go:2277) deliberately owns the FEW-
// implementor case — it resolves a qualified call whose method is on 1-3 classes
// at conf 0.4-0.6 (the "few candidates, more likely right" case). It SKIPS when a
// method is on >3 classes (too ambiguous), and unique_method (1.98) skips unless
// the name is globally unique. So a qualified call to a method defined on 4+ classes
// with an unproven receiver falls through to the last-chance block. It used to hit
// Strategy 2 and mint a PLAIN name_match onto an ARBITRARY same-named target — the
// least-honest label for the MOST-ambiguous case (matches the measured Rust residual:
// clone/new/map/push are each on dozens of types).
//
// The last-chance block now handles len(candidates)>1 by REACHABILITY (structural,
// NOT a builtin name-list): it PREFERS a reachable target for the pick, and mints the
// best pick (reachable-preferred, else best-of-all — connectivity is KEPT, never
// dropped, so Tier-2 langs with no import index and the demand-driven LSP pass are not
// starved: Fable #4) at a SPECULATIVE 0.2 floor with the honest EvidenceType
// name_match_qualified_unresolved (never plain name_match) so it never poses as a bare
// fact.
//
// Both cases are RED on the pre-E code, which minted a PLAIN name_match edge at
// computeConfidence (0.4 for 4 candidates), not name_match_qualified_unresolved at 0.2.
// `render` is on FOUR classes so impl_method (<=3) skips and the call reaches the
// backstop — exactly the real >3-implementor residual.
// ----------------------------------------------------------------------------

// Case 1 — NONE reachable => keep connectivity, FLOORED + honestly labeled (never a
// plain name_match, never a DROP). `render` on four classes in four unrelated packages
// the caller neither shares a directory with nor imports.
func TestResolve_E_MultiCandidateQualified_NoneReachable_FlooredNotBareNameMatch(t *testing.T) {
	files := []string{"pkg_a/a.py", "pkg_b/b.py", "pkg_c/c.py", "pkg_d/d.py", "pkg_e/e.py"}
	langs := []string{"python", "python", "python", "python", "python"}
	fm := BuildFileMap(files, langs)

	nodeIDs := map[string][]int64{
		"caller": {1},
		"Widget": {3}, "Gadget": {5}, "Sprocket": {7}, "Doohickey": {9},
		"render": {4, 6, 8, 10},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"pkg_a/a.py": {"caller": {1}},
		"pkg_b/b.py": {"Widget": {3}, "render": {4}},
		"pkg_c/c.py": {"Gadget": {5}, "render": {6}},
		"pkg_d/d.py": {"Sprocket": {7}, "render": {8}},
		"pkg_e/e.py": {"Doohickey": {9}, "render": {10}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", File: "pkg_a/a.py", Name: "caller"},
		3:  {Label: "Class", File: "pkg_b/b.py", Name: "Widget"},
		4:  {Label: "Method", File: "pkg_b/b.py", Name: "render", ParentID: 3},
		5:  {Label: "Class", File: "pkg_c/c.py", Name: "Gadget"},
		6:  {Label: "Method", File: "pkg_c/c.py", Name: "render", ParentID: 5},
		7:  {Label: "Class", File: "pkg_d/d.py", Name: "Sprocket"},
		8:  {Label: "Method", File: "pkg_d/d.py", Name: "render", ParentID: 7},
		9:  {Label: "Class", File: "pkg_e/e.py", Name: "Doohickey"},
		10: {Label: "Method", File: "pkg_e/e.py", Name: "render", ParentID: 9},
	}
	// unproven receiver `w` (no declared type / not a class / not self); `render`
	// is on 4 classes -> impl_method (<=3) skips, unique_method skips -> last-chance.
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "render", CalleeQualified: "w.render", Line: 5, File: "pkg_a/a.py"},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	var edge *ResolvedCall
	for i := range resolved {
		if resolved[i].TargetNodeID == 4 || resolved[i].TargetNodeID == 6 ||
			resolved[i].TargetNodeID == 8 || resolved[i].TargetNodeID == 10 {
			edge = &resolved[i]
		}
	}
	// Connectivity is KEPT (not dropped) so Tier-2 / LSP-recovery are not starved...
	if edge == nil {
		t.Fatalf("expected a floored render edge (connectivity kept), got %+v", resolved)
	}
	// ...but it must be HONESTLY LABELED (RED on pre-E, which minted a PLAIN name_match)...
	if edge.EvidenceType != "name_match_qualified_unresolved" {
		t.Errorf("EvidenceType = %q, want name_match_qualified_unresolved — a multi-candidate qualified-unresolved call must never pose as a bare name_match fact", edge.EvidenceType)
	}
	if edge.Method != "name_match" {
		t.Errorf("Method = %q, want name_match", edge.Method)
	}
	// ...and FLOORED to the 0.2 SPECULATIVE value (RED on pre-E's computeConfidence=0.4;
	// locks LIPI BUG 5 — a multi-candidate untyped receiver can never be a CANDIDATE fact).
	if edge.Confidence < 0.15 || edge.Confidence > 0.25 {
		t.Errorf("confidence = %.4f, want 0.20 (SPECULATIVE floor, below the 0.5 fact gate)", edge.Confidence)
	}
}

// Case 2 — exactly ONE reachable (same directory as the caller) => mint the best
// reachable, honestly labeled, and NEVER an unreachable candidate.
func TestResolve_E_MultiCandidateQualified_OneReachable_LabeledNotBareNameMatch(t *testing.T) {
	files := []string{"pkg/a.py", "pkg/b.py", "o1/c.py", "o2/d.py", "o3/e.py"}
	langs := []string{"python", "python", "python", "python", "python"}
	fm := BuildFileMap(files, langs)

	nodeIDs := map[string][]int64{
		"caller": {1},
		"Widget": {3}, "Gadget": {5}, "Sprocket": {7}, "Doohickey": {9},
		"render": {4, 6, 8, 10},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"pkg/a.py": {"caller": {1}},
		"pkg/b.py": {"Widget": {3}, "render": {4}}, // same dir as caller -> reachable
		"o1/c.py":  {"Gadget": {5}, "render": {6}},
		"o2/d.py":  {"Sprocket": {7}, "render": {8}},
		"o3/e.py":  {"Doohickey": {9}, "render": {10}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", File: "pkg/a.py", Name: "caller"},
		3:  {Label: "Class", File: "pkg/b.py", Name: "Widget"},
		4:  {Label: "Method", File: "pkg/b.py", Name: "render", ParentID: 3},
		5:  {Label: "Class", File: "o1/c.py", Name: "Gadget"},
		6:  {Label: "Method", File: "o1/c.py", Name: "render", ParentID: 5},
		7:  {Label: "Class", File: "o2/d.py", Name: "Sprocket"},
		8:  {Label: "Method", File: "o2/d.py", Name: "render", ParentID: 7},
		9:  {Label: "Class", File: "o3/e.py", Name: "Doohickey"},
		10: {Label: "Method", File: "o3/e.py", Name: "render", ParentID: 9},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "render", CalleeQualified: "w.render", Line: 5, File: "pkg/a.py"},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	var edge *ResolvedCall
	for i := range resolved {
		if resolved[i].TargetNodeID == 6 || resolved[i].TargetNodeID == 8 || resolved[i].TargetNodeID == 10 {
			t.Errorf("minted an edge to an UNREACHABLE candidate (%d) via %q — the reachability filter must exclude it", resolved[i].TargetNodeID, resolved[i].Method)
		}
		if resolved[i].TargetNodeID == 4 {
			edge = &resolved[i]
		}
	}
	if edge == nil {
		t.Fatalf("expected a floored edge to the reachable render (id4), got %+v", resolved)
	}
	if edge.EvidenceType != "name_match_qualified_unresolved" {
		t.Errorf("EvidenceType = %q, want name_match_qualified_unresolved — a multi-candidate qualified call must be labeled honestly, not as a bare name_match", edge.EvidenceType)
	}
	if edge.Confidence >= 0.5 {
		t.Errorf("confidence = %.2f — an untyped multi-candidate receiver must stay below the 0.5 fact floor", edge.Confidence)
	}
}
