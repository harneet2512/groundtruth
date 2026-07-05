package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// B1-#6 + receiver-type guard: when a method name exists on 2-3 classes (Alpha.run AND
// Beta.run), the receiver-BLIND impl_method rung (1.94) would pick the first class (Alpha).
// If the receiver's type is KNOWN via the assignment index — directly (`a = Beta()`) or
// through an alias chain (`b = a; c = b`) — resolution MUST yield the actual class (Beta),
// not the blind guess. Proves the 1.94 guard defers to receiver-aware 1.96, and that the
// alias fixpoint propagates the type across `b=a; c=b`.
func resolveRunReceiver(t *testing.T, assignments []parser.AssignmentRef, calleeQualified string) *ResolvedCall {
	t.Helper()
	fm := BuildFileMap([]string{"m.py"}, []string{"python"})
	// Alpha(10).run=1, Beta(20).run=2, enclosing function use(100).
	nodeIDs := map[string][]int64{"run": {1, 2}, "Alpha": {10}, "Beta": {20}, "use": {100}}
	fileNodeIDs := map[string]map[string][]int64{
		"m.py": {"run": {1, 2}, "Alpha": {10}, "Beta": {20}, "use": {100}},
	}
	meta := map[int64]NodeMeta{
		1:   {Label: "Method", Name: "run", File: "m.py", ParentID: 10, StartLine: 2},
		2:   {Label: "Method", Name: "run", File: "m.py", ParentID: 20, StartLine: 5},
		10:  {Label: "Class", Name: "Alpha", File: "m.py", StartLine: 1},
		20:  {Label: "Class", Name: "Beta", File: "m.py", StartLine: 4},
		100: {Label: "Function", Name: "use", File: "m.py", StartLine: 7},
	}
	SetAssignmentIndex(BuildAssignmentIndex(assignments))
	defer SetAssignmentIndex(nil)

	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "run", CalleeQualified: calleeQualified, Line: 10, File: "m.py"},
	}
	resolved := Resolve(calls, nodeIDs, fileNodeIDs, []int64{100}, nil, fm, meta)
	for i := range resolved {
		if resolved[i].SourceNodeID == 100 && (resolved[i].TargetNodeID == 1 || resolved[i].TargetNodeID == 2) {
			return &resolved[i]
		}
	}
	return nil
}

func TestResolve_B6_DirectReceiverTypeBeatsBlindGuess(t *testing.T) {
	// a = Beta(); a.run()  →  must resolve to Beta.run (id 2), NOT the blind Alpha (id 1).
	got := resolveRunReceiver(t,
		[]parser.AssignmentRef{{VarName: "a", TypeName: "Beta", Scope: "use", File: "m.py"}},
		"a.run")
	if got == nil {
		t.Fatalf("a.run() did not resolve to any run method")
	}
	if got.TargetNodeID != 2 {
		t.Errorf("a=Beta(); a.run() resolved to id %d; want Beta.run id 2 (receiver-type, not blind Alpha guess)", got.TargetNodeID)
	}
}

// Fable RS6 (RED→GREEN): the assignment-flow fallback (assignments.go ResolveQualifiedCall)
// resolves a local var from a file-global last-write-wins when NO same-scope write exists —
// i.e. it borrows a type written in a DIFFERENT function. Pre-fix it minted that as 0.9
// CERTIFIED "assignment_tracked", identical to an in-scope proof. The scopeProven flag now
// demotes the cross-function fallback to 0.6 CANDIDATE "assignment_tracked_crossscope". The
// in-scope case (TestResolve_B6_DirectReceiverTypeBeatsBlindGuess, scope=="use") still mints 0.9.
func TestResolve_RS6_CrossScopeAssignmentDemotedNotCertified(t *testing.T) {
	// `a = Beta()` is written in function `other`; `a.run()` is called from `use`.
	got := resolveRunReceiver(t,
		[]parser.AssignmentRef{{VarName: "a", TypeName: "Beta", Scope: "other", File: "m.py"}},
		"a.run")
	if got == nil {
		t.Fatalf("a.run() did not resolve via the cross-scope fallback")
	}
	if got.Confidence >= 0.9 || got.TrustTier == "CERTIFIED" {
		t.Errorf("RS6: a cross-scope assignment minted a CERTIFIED fact (conf=%.2f tier=%q ev=%q) — "+
			"a type from a DIFFERENT function's write is not proven at this call site",
			got.Confidence, got.TrustTier, got.EvidenceType)
	}
	if got.EvidenceType != "assignment_tracked_crossscope" {
		t.Errorf("RS6: evidence=%q; want assignment_tracked_crossscope (the demoted cross-scope label)", got.EvidenceType)
	}
}

func TestResolve_B6_AliasChainPropagatesReceiverType(t *testing.T) {
	// a = Beta(); b = a; c = b; c.run()  →  Beta.run (id 2) via the alias fixpoint.
	got := resolveRunReceiver(t,
		[]parser.AssignmentRef{
			{VarName: "a", TypeName: "Beta", Scope: "use", File: "m.py"},
			{VarName: "b", AliasOf: "a", Scope: "use", File: "m.py"},
			{VarName: "c", AliasOf: "b", Scope: "use", File: "m.py"},
		},
		"c.run")
	if got == nil {
		t.Fatalf("c.run() did not resolve through the alias chain")
	}
	if got.TargetNodeID != 2 {
		t.Errorf("alias chain a=Beta();b=a;c=b; c.run() resolved to id %d; want Beta.run id 2 (alias fixpoint)", got.TargetNodeID)
	}
}
