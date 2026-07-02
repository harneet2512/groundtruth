package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// B-2 — resolver Strategy 1 (same_file) must be RECEIVER-AWARE, like Strategy 1.5 (B1).
// A QUALIFIED call `self.session.get()` (CalleeName="get", CalleeQualified="self.session.get")
// must NOT bind a bare same-file `def get` at method="same_file"/1.0/CERTIFIED — an attribute
// call on a receiver can never bind a bare same-file name; only the receiver-typing rungs
// (1.75 self/this, 2b field-type, 1.94a/1.95/1.96) may resolve it. UNQUALIFIED `get()` still
// resolves same_file/1.0 (Strategy 1 unchanged for unqualified).
// Mutation: dropping the isUnqualified guard on the unambiguous branch re-mints the
// same_file/CERTIFIED launder for the qualified call (RED).
func TestResolve_B2_Strategy1QualifiedDoesNotBindSameFile(t *testing.T) {
	fm := BuildFileMap([]string{"app/svc.py"}, []string{"python"})
	nodeIDs := map[string][]int64{"caller": {1}, "get": {2}}
	fileNodeIDs := map[string]map[string][]int64{
		"app/svc.py": {"caller": {1}, "get": {2}},
	}
	callerIDs := []int64{1}
	meta := map[int64]NodeMeta{
		1: {Label: "Method", Name: "caller", File: "app/svc.py"},
		2: {Label: "Function", Name: "get", File: "app/svc.py"},
	}

	// QUALIFIED (multi-hop receiver): must NOT bind same_file/CERTIFIED.
	qCalls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "self.session.get", Line: 5, File: "app/svc.py"},
	}
	for _, r := range Resolve(qCalls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta) {
		if r.TargetNodeID == 2 && r.Method == "same_file" {
			t.Errorf("B-2: qualified self.session.get() bound same_file/%.2f/%s to get(2) — "+
				"receiver-blind launder; a qualified call must fall to the receiver-typing rungs",
				r.Confidence, r.TrustTier)
		}
	}

	// UNQUALIFIED positive control: must STILL resolve same_file/1.0.
	uCalls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "get", Line: 6, File: "app/svc.py"},
	}
	var sawSameFile bool
	for _, r := range Resolve(uCalls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta) {
		if r.TargetNodeID == 2 && r.Method == "same_file" && r.Confidence == 1.0 {
			sawSameFile = true
		}
	}
	if !sawSameFile {
		t.Errorf("unqualified get() must still resolve same_file/1.0 (Strategy 1 unchanged for unqualified)")
	}
}
