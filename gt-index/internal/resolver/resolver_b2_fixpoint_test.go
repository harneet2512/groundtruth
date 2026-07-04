package resolver

import (
	"os"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ---------------------------------------------------------------------------
// B2 — worklist FIXPOINT over the type facts already stored (GT_TYPEFLOW_FIXPOINT,
// default off).
//
// The single-shot ladder resolves `x = factory(); x.m()` only when factory has a
// directly-knowable return type. A CHAINED flow
//
//	a = make(); b = a.foo(); b.bar()
//
// never resolves b.bar(): the parser stores `b = a.foo()` as a ViaReturn assignment
// whose TypeName is the QUALIFIED RHS "a.foo" (not a function node), so Strategy 1.96
// looks up nodeIDs["a.foo"] (nothing) and b's type is never inferred. b's type only
// becomes knowable AFTER a's type (A) is inferred and foo's return type (B) is resolved
// on class A — a SECOND propagation round. Under the flag a pre-pass iterates the SAME
// facts (assignmentIndex + returnShapeIndex + methodsByClass/CHA) to a fixpoint and a new
// rung (after 1.96) resolves b.bar() to type_flow / evidence "fixpoint" — the same
// categorical FACT boundary as 1.96, never a name_match promotion.
//
// Guardrails proven here: OFF leaves the chained call name_match (byte-identical),
// ON resolves it to type_flow/fixpoint with the RIGHT target, determinism both modes,
// ≥2 languages (Python + Go). The MUTATION companion neuters re-propagation (runs the
// body once) and asserts the depth-1 chain reverts to name_match — proving the
// re-propagation is load-bearing.
// ---------------------------------------------------------------------------

// chainB2Fixture builds a repo with a chained receiver flow in one file:
//
//	func caller():                # scope "run"
//	    a = make()                # make() -> class A
//	    b = a.foo()               # A.foo() -> class B
//	    b.bar()                   # <-- CHAINED CALL: resolves only once b:B is inferred
//
// bar is defined in FOUR classes (B + 3 decoys) so neither few-implementor (1.94, cap 3)
// nor unique-method (1.98) can fire — OFF the call is a name_match; ON the fixpoint proves
// the receiver is B and resolves to B.bar (id 6). lang selects the file extension / names.
func chainB2Fixture(lang string) (calls []parser.CallRef, nodeIDs map[string][]int64,
	fileNodeIDs map[string]map[string][]int64, callerIDs []int64,
	imports []parser.ImportRef, fm map[string][]string, meta map[int64]NodeMeta,
	assignments []parser.AssignmentRef) {

	file := "app.py"
	classLabel := "Class"
	if lang == "go" {
		file = "app.go"
		classLabel = "Struct"
	}
	fm = BuildFileMap([]string{file}, []string{lang})

	// Node layout (all in one file). bar in classes B(6), C(8), D(10), E(12) = 4 classes.
	nodeIDs = map[string][]int64{
		"run":  {1},
		"make": {2},
		"A":    {3},
		"foo":  {4},
		"B":    {5},
		"bar":  {6, 8, 10, 12},
		"C":    {7},
		"D":    {9},
		"E":    {11},
	}
	fileNodeIDs = map[string]map[string][]int64{
		file: {
			"run": {1}, "make": {2}, "A": {3}, "foo": {4}, "B": {5},
			"bar": {6, 8, 10, 12}, "C": {7}, "D": {9}, "E": {11},
		},
	}
	meta = map[int64]NodeMeta{
		1:  {Label: "Function", File: file, Name: "run", StartLine: 10},
		2:  {Label: "Function", File: file, Name: "make", ReturnType: "A", StartLine: 20}, // make() -> A
		3:  {Label: classLabel, File: file, Name: "A", StartLine: 30},
		4:  {Label: "Method", File: file, Name: "foo", ParentID: 3, ReturnType: "B", StartLine: 31}, // A.foo() -> B
		5:  {Label: classLabel, File: file, Name: "B", StartLine: 40},
		6:  {Label: "Method", File: file, Name: "bar", ParentID: 5, StartLine: 41}, // B.bar (the TARGET)
		7:  {Label: classLabel, File: file, Name: "C", StartLine: 50},
		8:  {Label: "Method", File: file, Name: "bar", ParentID: 7, StartLine: 51},
		9:  {Label: classLabel, File: file, Name: "D", StartLine: 60},
		10: {Label: "Method", File: file, Name: "bar", ParentID: 9, StartLine: 61},
		11: {Label: classLabel, File: file, Name: "E", StartLine: 70},
		12: {Label: "Method", File: file, Name: "bar", ParentID: 11, StartLine: 71},
	}
	// The chained call site. Caller = run (id 1).
	calls = []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "bar", CalleeQualified: "b.bar", Line: 13, File: file},
	}
	callerIDs = []int64{1}
	// The two assignments the parser would emit for the chain (constructed as the parser
	// records them: bare factory `a = make()` and qualified chain `b = a.foo()`).
	assignments = []parser.AssignmentRef{
		{VarName: "a", TypeName: "make", ViaReturn: true, Scope: "run", File: file, Line: 11},
		{VarName: "b", TypeName: "a.foo", ViaReturn: true, Scope: "run", File: file, Line: 12},
	}
	return
}

// barEdge returns the resolved edge for the chained b.bar() call (caller 1 → any bar).
func barEdge(rcs []ResolvedCall) (ResolvedCall, bool) {
	for _, r := range rcs {
		if r.SourceNodeID == 1 && (r.TargetNodeID == 6 || r.TargetNodeID == 8 ||
			r.TargetNodeID == 10 || r.TargetNodeID == 12) {
			return r, true
		}
	}
	return ResolvedCall{}, false
}

// setB2Globals installs the assignment index and clears every other global index so ONLY
// the assignment/return-shape path (and the fixpoint over it) is in play. Returns a reset.
func setB2Globals(assignments []parser.AssignmentRef) func() {
	SetAssignmentIndex(BuildAssignmentIndex(assignments))
	SetParamTypeIndex(nil)
	SetFieldTypeIndex(nil)
	SetReturnShapeIndex(nil)
	SetInheritanceMap(nil)
	return func() {
		SetAssignmentIndex(nil)
	}
}

func TestResolve_TypeflowFixpoint_ChainedResolves(t *testing.T) {
	for _, lang := range []string{"python", "go"} {
		t.Run(lang, func(t *testing.T) {
			calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta, assignments := chainB2Fixture(lang)
			reset := setB2Globals(assignments)
			defer reset()

			// OFF: b.bar() is an ambiguous name_match (receiver type never inferred).
			os.Unsetenv("GT_TYPEFLOW_FIXPOINT")
			off := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
			r, ok := barEdge(off)
			if !ok {
				t.Fatalf("OFF: expected a name_match edge for b.bar(); got none (all=%+v)", off)
			}
			if r.Method != "name_match" {
				t.Fatalf("OFF: b.bar() must be name_match (receiver unproven); got method=%q evidence=%q target=%d",
					r.Method, r.EvidenceType, r.TargetNodeID)
			}

			// ON: the fixpoint proves b:B → b.bar() resolves to B.bar (id 6) via type_flow/fixpoint.
			os.Setenv("GT_TYPEFLOW_FIXPOINT", "1")
			defer os.Unsetenv("GT_TYPEFLOW_FIXPOINT")
			on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
			r, ok = barEdge(on)
			if !ok {
				t.Fatalf("ON: expected an edge for b.bar(); got none (all=%+v)", on)
			}
			if r.TargetNodeID != 6 {
				t.Errorf("ON: b.bar() must resolve to the RIGHT target B.bar (id 6); got target=%d method=%q",
					r.TargetNodeID, r.Method)
			}
			if r.Method != "type_flow" || r.EvidenceType != "fixpoint" {
				t.Errorf("ON: b.bar() must be type_flow/fixpoint; got method=%q evidence=%q", r.Method, r.EvidenceType)
			}
			// conf 0.9 == CERTIFIED by the tier table — the SAME categorical boundary as
			// 1.96 type_flow (assignment_tracked / field_type), NOT a name_match promotion.
			if r.Confidence != 0.9 {
				t.Errorf("ON: fixpoint edge must carry the type_flow conf 0.9 (1.96's FACT boundary); got %v", r.Confidence)
			}
		})
	}
}

// Byte-identical-off: with the flag unset the OFF edge set is identical across two REAL
// Resolve calls (determinism) and is the pre-change set (b.bar() name_match). Drives the
// real Resolve twice.
func TestResolve_TypeflowFixpoint_ByteIdenticalOff(t *testing.T) {
	os.Unsetenv("GT_TYPEFLOW_FIXPOINT")
	for _, lang := range []string{"python", "go"} {
		calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta, assignments := chainB2Fixture(lang)
		reset := setB2Globals(assignments)
		a := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
		b := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
		reset()
		if len(a) != len(b) {
			t.Fatalf("[%s] OFF determinism: edge count differs %d vs %d", lang, len(a), len(b))
		}
		for i := range a {
			if a[i] != b[i] {
				t.Fatalf("[%s] OFF determinism: edge %d differs %+v vs %+v", lang, i, a[i], b[i])
			}
		}
	}
}

// ON determinism: two indexings with the flag on are byte-identical (the fixpoint result
// is a deterministic map; the consuming rung emits on the ordered call slice).
func TestResolve_TypeflowFixpoint_OnDeterminism(t *testing.T) {
	os.Setenv("GT_TYPEFLOW_FIXPOINT", "1")
	defer os.Unsetenv("GT_TYPEFLOW_FIXPOINT")
	for _, lang := range []string{"python", "go"} {
		calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta, assignments := chainB2Fixture(lang)
		reset := setB2Globals(assignments)
		a := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
		b := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
		reset()
		if len(a) != len(b) {
			t.Fatalf("[%s] ON determinism: edge count differs %d vs %d", lang, len(a), len(b))
		}
		for i := range a {
			if a[i] != b[i] {
				t.Fatalf("[%s] ON determinism: edge %d differs %+v vs %+v", lang, i, a[i], b[i])
			}
		}
	}
}

// MUTATION companion. The re-propagation is what lets a depth-1 chain resolve: a's type is
// seeded in round 1, b's type (which reads a's) is inferred in round 2. Neuter the worklist
// so it runs the body ONCE (typeflowFixpointMaxIters = 1) and the chained call must REVERT
// to name_match — its resolution proves re-propagation is load-bearing. Restores the cap.
func TestResolve_TypeflowFixpoint_Mutation_BodyOnceReverts(t *testing.T) {
	os.Setenv("GT_TYPEFLOW_FIXPOINT", "1")
	defer os.Unsetenv("GT_TYPEFLOW_FIXPOINT")
	saved := typeflowFixpointMaxIters
	typeflowFixpointMaxIters = 1 // NEUTER: run the propagation body once (no re-propagation)
	defer func() { typeflowFixpointMaxIters = saved }()

	for _, lang := range []string{"python", "go"} {
		calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta, assignments := chainB2Fixture(lang)
		reset := setB2Globals(assignments)
		got := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
		reset()
		r, ok := barEdge(got)
		if !ok {
			t.Fatalf("[%s] MUTATION: expected some edge for b.bar(); got none", lang)
		}
		if r.Method == "type_flow" && r.EvidenceType == "fixpoint" {
			t.Errorf("[%s] MUTATION GUARD: with re-propagation neutered (1 iteration) the depth-1 chain "+
				"b.bar() must NOT resolve via the fixpoint — its type_flow/fixpoint resolution proves the "+
				"worklist re-propagation was not actually load-bearing", lang)
		}
		if r.Method != "name_match" {
			t.Errorf("[%s] MUTATION: neutered fixpoint must leave b.bar() as name_match; got method=%q", lang, r.Method)
		}
	}
}
