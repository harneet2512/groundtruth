package resolver

import (
	"os"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ---------------------------------------------------------------------------
// F8 — B2 same-name-method SCOPE COLLAPSE (the correct-or-quiet seed).
//
// AssignmentRef.Scope is only the BARE enclosing-function name (parser.go:122),
// and the B2 consumer keys its lookup by metaMap[callerID].Name — the same bare
// name. Two same-named methods in one file (Foo.run / Bar.run) therefore COLLAPSE
// onto one scope key "run": the fixpoint's per-scope filter merges BOTH methods'
// assignments, pickVarType takes the LAST write, and a depth-1 chained receiver
// typed in Bar.run mints a WRONG 0.9 CERTIFIED type_flow/fixpoint edge for the
// call sitting in Foo.run (the F4 depth decay only reaches depth≥2 — a depth-1
// chain is CERTIFIED). The parser records nothing finer than the bare name, so
// the fix ABSTAINS: any (scope,var) pair whose scope name has ≥2 function/method
// definitions in the file is skipped — no key, no consumer hit, no guess.
//
// RED (pre-fix): the ambiguous fixture emits type_flow/fixpoint conf 0.9 onto the
// WRONG class's method (target 14). GREEN (post-fix): the fixpoint abstains — the
// call falls back to today's name_match — while the UNIQUE-scope control still
// resolves to the RIGHT target with 0.9 (no over-abstain).
// ---------------------------------------------------------------------------

// sameNameScopeFixture builds ONE file with two classes whose methods share the
// bare name `run` (ambiguous=true) or carry distinct names runFoo/runBar
// (ambiguous=false). Each method contains a differently-typed depth-1 chain:
//
//	Foo.run:  a = mkA(); b = a.fooA()   → b: B   (mkA→A, A.fooA→B)
//	Bar.run:  a = mkC(); b = a.fooC()   → b: DD  (mkC→CC, CC.fooC→DD)
//
// The probed CALL `b.bar()` sits in Foo.run — its TRUE receiver is B, so the only
// correct CERTIFIED resolution is B.bar (id 6). Bar.run's assignments are listed
// LAST so the collapsed last-write pick resolves b to DD and the pre-fix bug
// lands the edge on DD.bar (id 14) — provably wrong for Foo.run. bar exists on
// 4 classes (B, DD, E, F) so 1.94/1.98 cannot fire and OFF stays name_match.
func sameNameScopeFixture(ambiguous bool) (calls []parser.CallRef, nodeIDs map[string][]int64,
	fileNodeIDs map[string]map[string][]int64, callerIDs []int64,
	imports []parser.ImportRef, fm map[string][]string, meta map[int64]NodeMeta,
	assignments []parser.AssignmentRef) {

	file := "app.py"
	fm = BuildFileMap([]string{file}, []string{"python"})

	fooScope, barScope := "run", "run"
	if !ambiguous {
		fooScope, barScope = "runFoo", "runBar"
	}

	nodeIDs = map[string][]int64{
		fooScope: {1},
		"mkA":    {2},
		"A":      {3},
		"fooA":   {4},
		"B":      {5},
		"bar":    {6, 14, 16, 18},
		"Foo":    {20},
		"Bar":    {21},
		"mkC":    {22},
		"CC":     {7},
		"fooC":   {23},
		"DD":     {9},
		"E":      {15},
		"F":      {17},
	}
	if ambiguous {
		nodeIDs["run"] = []int64{1, 13}
	} else {
		nodeIDs[barScope] = []int64{13}
	}
	fileNodes := map[string][]int64{
		"mkA": {2}, "A": {3}, "fooA": {4}, "B": {5},
		"bar": {6, 14, 16, 18}, "Foo": {20}, "Bar": {21},
		"mkC": {22}, "CC": {7}, "fooC": {23}, "DD": {9}, "E": {15}, "F": {17},
	}
	if ambiguous {
		fileNodes["run"] = []int64{1, 13}
	} else {
		fileNodes[fooScope] = []int64{1}
		fileNodes[barScope] = []int64{13}
	}
	fileNodeIDs = map[string]map[string][]int64{file: fileNodes}

	meta = map[int64]NodeMeta{
		1:  {Label: "Method", File: file, Name: fooScope, ParentID: 20, StartLine: 11},
		13: {Label: "Method", File: file, Name: barScope, ParentID: 21, StartLine: 31},
		20: {Label: "Class", File: file, Name: "Foo", StartLine: 10},
		21: {Label: "Class", File: file, Name: "Bar", StartLine: 30},
		2:  {Label: "Function", File: file, Name: "mkA", ReturnType: "A", StartLine: 50},
		3:  {Label: "Class", File: file, Name: "A", StartLine: 60},
		4:  {Label: "Method", File: file, Name: "fooA", ParentID: 3, ReturnType: "B", StartLine: 61},
		5:  {Label: "Class", File: file, Name: "B", StartLine: 70},
		6:  {Label: "Method", File: file, Name: "bar", ParentID: 5, StartLine: 71}, // the TRUE target for Foo.run's b
		22: {Label: "Function", File: file, Name: "mkC", ReturnType: "CC", StartLine: 80},
		7:  {Label: "Class", File: file, Name: "CC", StartLine: 90},
		23: {Label: "Method", File: file, Name: "fooC", ParentID: 7, ReturnType: "DD", StartLine: 91},
		9:  {Label: "Class", File: file, Name: "DD", StartLine: 100},
		14: {Label: "Method", File: file, Name: "bar", ParentID: 9, StartLine: 101}, // the WRONG class's bar
		15: {Label: "Class", File: file, Name: "E", StartLine: 110},
		16: {Label: "Method", File: file, Name: "bar", ParentID: 15, StartLine: 111},
		17: {Label: "Class", File: file, Name: "F", StartLine: 120},
		18: {Label: "Method", File: file, Name: "bar", ParentID: 17, StartLine: 121},
	}

	// The probed chained call sits in Foo.run (caller id 1).
	calls = []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "bar", CalleeQualified: "b.bar", Line: 13, File: file},
	}
	callerIDs = []int64{1}

	// Foo.run's chain FIRST, Bar.run's LAST — the collapsed last-write pick under an
	// ambiguous scope resolves a→CC / b→DD, wrong for the call in Foo.run.
	assignments = []parser.AssignmentRef{
		{VarName: "a", TypeName: "mkA", ViaReturn: true, Scope: fooScope, File: file, Line: 12},
		{VarName: "b", TypeName: "a.fooA", ViaReturn: true, Scope: fooScope, File: file, Line: 13},
		{VarName: "a", TypeName: "mkC", ViaReturn: true, Scope: barScope, File: file, Line: 32},
		{VarName: "b", TypeName: "a.fooC", ViaReturn: true, Scope: barScope, File: file, Line: 33},
	}
	return
}

func sameNameBarEdge(rcs []ResolvedCall) (ResolvedCall, bool) {
	for _, r := range rcs {
		if r.SourceNodeID == 1 && (r.TargetNodeID == 6 || r.TargetNodeID == 14 ||
			r.TargetNodeID == 16 || r.TargetNodeID == 18) {
			return r, true
		}
	}
	return ResolvedCall{}, false
}

func TestResolve_TypeflowFixpoint_SameNameScopeAbstains(t *testing.T) {
	os.Setenv("GT_TYPEFLOW_FIXPOINT", "1")
	defer os.Unsetenv("GT_TYPEFLOW_FIXPOINT")

	// AMBIGUOUS: two methods named `run` in the file → the fixpoint must ABSTAIN.
	// Pre-fix this emitted type_flow/fixpoint conf 0.9 onto DD.bar (14) — a wrong
	// CERTIFIED fact for the call in Foo.run (whose b is a B).
	calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta, assignments := sameNameScopeFixture(true)
	reset := setB2Globals(assignments)
	got := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	reset()
	if r, ok := sameNameBarEdge(got); ok {
		if r.Method == "type_flow" && r.EvidenceType == "fixpoint" {
			t.Errorf("SEED: ambiguous scope 'run' must ABSTAIN — got a %s/%s conf=%v edge onto target %d "+
				"(a guessed CERTIFIED across two same-named methods)", r.Method, r.EvidenceType, r.Confidence, r.TargetNodeID)
		}
		if r.Method != "name_match" {
			t.Errorf("SEED: with the fixpoint abstaining, b.bar() must stay today's name_match; got %q", r.Method)
		}
	}

	// UNIQUE control: distinct method names (runFoo/runBar) → the SAME chain in the
	// caller resolves to the RIGHT target B.bar (6) at 0.9 — no over-abstain.
	calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta, assignments = sameNameScopeFixture(false)
	reset = setB2Globals(assignments)
	got = Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	reset()
	r, ok := sameNameBarEdge(got)
	if !ok {
		t.Fatalf("CONTROL: unique-scope chain must resolve; got no bar edge")
	}
	if r.TargetNodeID != 6 || r.Method != "type_flow" || r.EvidenceType != "fixpoint" || r.Confidence != 0.9 {
		t.Errorf("CONTROL: unique-scope chain must resolve to B.bar(6) via type_flow/fixpoint@0.9; got target=%d method=%q evidence=%q conf=%v",
			r.TargetNodeID, r.Method, r.EvidenceType, r.Confidence)
	}
}
