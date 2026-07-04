package resolver

import (
	"os"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ---------------------------------------------------------------------------
// B2 fixpoint DEPTH-rung defect fixes (GT_TYPEFLOW_FIXPOINT, default off).
//
//   F1 — scope collapse: the pre-pass picked ONE global type per var and keyed it by the
//        picked entry's scope, so a var name reused in two functions resolved in at most
//        ONE scope (the other MISSED / could leak the wrong class). Fixed: resolve PER
//        scope, keyed to the CALL's own scope.
//   F4 — depth decay: every chained hop stamped 0.9/CERTIFIED regardless of chain depth.
//        Fixed: a depth-1 hop keeps 0.9/CERTIFIED (1.96 parity); a deeper chain (round >=2)
//        is demoted to CANDIDATE, strictly non-increasing with depth.
//   F5 — `::` receiver: the consumer split only on `.`, so a Rust `::`-qualified chained
//        call the pre-pass populated was never consumed. Fixed: split on `.` OR `::`.
// ---------------------------------------------------------------------------

// scopeCollapseFixture builds ONE file with the SAME local var name `x` in TWO differently-
// named functions, each a CHAINED receiver flow (so only the fixpoint — not the single-shot
// ladder — can type x), receivers of DIFFERENT classes with DIFFERENT chained methods:
//
//	func methodA():                # scope "methodA"
//	    a1 = makeA()               # makeA() -> MakerA
//	    x  = a1.foo()              # MakerA.foo() -> ClassA
//	    x.bar()                    # <-- must resolve on ClassA (id 10)
//	func methodB():                # scope "methodB"
//	    b1 = makeB()               # makeB() -> MakerB
//	    x  = b1.qux()              # MakerB.qux() -> ClassB
//	    x.baz()                    # <-- must resolve on ClassB (id 12)
//
// bar lives in 4 classes (ClassA + P,Q,R) and baz in 4 (ClassB + P,Q,R) so neither the
// few-implementor (1.94, cap 3) nor unique-method (1.98) rung can fire — only the fixpoint,
// which must key x PER SCOPE. methodA's x-assignment is emitted BEFORE methodB's, so the old
// global last-write pick keyed only methodB's x → methodA's x.bar() MISSED (the RED).
func scopeCollapseFixture(lang string) (calls []parser.CallRef, nodeIDs map[string][]int64,
	fileNodeIDs map[string]map[string][]int64, callerIDs []int64,
	imports []parser.ImportRef, fm map[string][]string, meta map[int64]NodeMeta,
	assignments []parser.AssignmentRef) {

	file := "app.py"
	cl := "Class"
	if lang == "go" {
		file = "app.go"
		cl = "Struct"
	}
	fm = BuildFileMap([]string{file}, []string{lang})

	nodeIDs = map[string][]int64{
		"methodA": {1}, "methodB": {2},
		"makeA": {3}, "makeB": {4},
		"MakerA": {5}, "foo": {6},
		"MakerB": {7}, "qux": {8},
		"ClassA": {9}, "ClassB": {11},
		"P": {13}, "Q": {16}, "R": {19},
		"bar": {10, 14, 17, 20},
		"baz": {12, 15, 18, 21},
	}
	fileNodeIDs = map[string]map[string][]int64{file: {
		"methodA": {1}, "methodB": {2}, "makeA": {3}, "makeB": {4},
		"MakerA": {5}, "foo": {6}, "MakerB": {7}, "qux": {8},
		"ClassA": {9}, "ClassB": {11}, "P": {13}, "Q": {16}, "R": {19},
		"bar": {10, 14, 17, 20}, "baz": {12, 15, 18, 21},
	}}
	meta = map[int64]NodeMeta{
		1:  {Label: "Function", File: file, Name: "methodA", StartLine: 10},
		2:  {Label: "Function", File: file, Name: "methodB", StartLine: 20},
		3:  {Label: "Function", File: file, Name: "makeA", ReturnType: "MakerA", StartLine: 30},
		4:  {Label: "Function", File: file, Name: "makeB", ReturnType: "MakerB", StartLine: 31},
		5:  {Label: cl, File: file, Name: "MakerA", StartLine: 40},
		6:  {Label: "Method", File: file, Name: "foo", ParentID: 5, ReturnType: "ClassA", StartLine: 41},
		7:  {Label: cl, File: file, Name: "MakerB", StartLine: 50},
		8:  {Label: "Method", File: file, Name: "qux", ParentID: 7, ReturnType: "ClassB", StartLine: 51},
		9:  {Label: cl, File: file, Name: "ClassA", StartLine: 60},
		10: {Label: "Method", File: file, Name: "bar", ParentID: 9, StartLine: 61},
		11: {Label: cl, File: file, Name: "ClassB", StartLine: 70},
		12: {Label: "Method", File: file, Name: "baz", ParentID: 11, StartLine: 71},
		13: {Label: cl, File: file, Name: "P", StartLine: 80},
		14: {Label: "Method", File: file, Name: "bar", ParentID: 13, StartLine: 81},
		15: {Label: "Method", File: file, Name: "baz", ParentID: 13, StartLine: 82},
		16: {Label: cl, File: file, Name: "Q", StartLine: 90},
		17: {Label: "Method", File: file, Name: "bar", ParentID: 16, StartLine: 91},
		18: {Label: "Method", File: file, Name: "baz", ParentID: 16, StartLine: 92},
		19: {Label: cl, File: file, Name: "R", StartLine: 100},
		20: {Label: "Method", File: file, Name: "bar", ParentID: 19, StartLine: 101},
		21: {Label: "Method", File: file, Name: "baz", ParentID: 19, StartLine: 102},
	}
	// methodA's chained call (caller id 1), methodB's chained call (caller id 2).
	calls = []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "bar", CalleeQualified: "x.bar", Line: 13, File: file},
		{CallerNodeIdx: 1, CalleeName: "baz", CalleeQualified: "x.baz", Line: 23, File: file},
	}
	callerIDs = []int64{1, 2}
	// methodA's assignments FIRST so the old global last-write pick chose methodB's x.
	assignments = []parser.AssignmentRef{
		{VarName: "a1", TypeName: "makeA", ViaReturn: true, Scope: "methodA", File: file, Line: 11},
		{VarName: "x", TypeName: "a1.foo", ViaReturn: true, Scope: "methodA", File: file, Line: 12},
		{VarName: "b1", TypeName: "makeB", ViaReturn: true, Scope: "methodB", File: file, Line: 21},
		{VarName: "x", TypeName: "b1.qux", ViaReturn: true, Scope: "methodB", File: file, Line: 22},
	}
	return
}

// F1 RED→GREEN. Pre-fix (global last-write pick keyed to one scope), methodA's x.bar()
// has NO type_flow/fixpoint edge to ClassA.bar — it missed. Post-fix, BOTH scopes' x
// resolve on THEIR OWN class, and neither leaks the other's class.
func TestResolve_TypeflowFixpoint_F1_PerScopeNoCollapse(t *testing.T) {
	os.Setenv("GT_TYPEFLOW_FIXPOINT", "1")
	defer os.Unsetenv("GT_TYPEFLOW_FIXPOINT")
	for _, lang := range []string{"python", "go"} {
		t.Run(lang, func(t *testing.T) {
			calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta, assignments := scopeCollapseFixture(lang)
			reset := setB2Globals(assignments)
			defer reset()
			on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)

			// methodA (caller 1): x.bar() resolves on ITS OWN class ClassA.bar (id 10).
			ra, ok := hasEdge(on, 1, 10)
			if !ok || ra.Method != "type_flow" || ra.EvidenceType != "fixpoint" {
				t.Fatalf("F1: methodA x.bar() must resolve on ClassA.bar(10) via type_flow/fixpoint; got %+v ok=%v (all=%+v)", ra, ok, on)
			}
			// methodB (caller 2): x.baz() resolves on ClassB.baz (id 12).
			rb, ok := hasEdge(on, 2, 12)
			if !ok || rb.Method != "type_flow" || rb.EvidenceType != "fixpoint" {
				t.Fatalf("F1: methodB x.baz() must resolve on ClassB.baz(12) via type_flow/fixpoint; got %+v ok=%v", rb, ok)
			}
			// Cross-scope guard: NO fixpoint edge from methodA's call into ClassB, nor
			// methodB's into ClassA. A leak would emit B's class at 0.9 for A.
			for _, r := range on {
				if r.EvidenceType != "fixpoint" {
					continue
				}
				if r.SourceNodeID == 1 && r.TargetNodeID == 12 {
					t.Errorf("F1 LEAK: methodA resolved onto ClassB.baz(12) — cross-scope wrong class")
				}
				if r.SourceNodeID == 2 && r.TargetNodeID == 10 {
					t.Errorf("F1 LEAK: methodB resolved onto ClassA.bar(10) — cross-scope wrong class")
				}
			}
		})
	}
}

// depthChainFixture builds a depth-2 chain in ONE scope plus a depth-1 and depth-2 call:
//
//	func run():                    # scope "run"
//	    a = make()                 # make() -> A                 (base seed,  round 0)
//	    b = a.foo()                # A.foo() -> B                (depth-1,    round 1)
//	    c = b.bar()                # B.bar() -> C                (depth-2,    round 2)
//	    b.mb()                     # <-- depth-1 receiver: 0.9 / CERTIFIED
//	    c.mc()                     # <-- depth-2 receiver: demoted CANDIDATE
//
// mb lives in 4 classes (B + P,Q,R), mc in 4 (C + P,Q,R) so only the fixpoint resolves them.
func depthChainFixture(lang string) (calls []parser.CallRef, nodeIDs map[string][]int64,
	fileNodeIDs map[string]map[string][]int64, callerIDs []int64,
	imports []parser.ImportRef, fm map[string][]string, meta map[int64]NodeMeta,
	assignments []parser.AssignmentRef) {

	file := "chain.py"
	cl := "Class"
	if lang == "go" {
		file = "chain.go"
		cl = "Struct"
	}
	fm = BuildFileMap([]string{file}, []string{lang})

	nodeIDs = map[string][]int64{
		"run": {1}, "make": {2},
		"A": {3}, "foo": {4},
		"B": {5}, "bar": {6},
		"mb": {7, 11, 14, 17},
		"C":  {8}, "mc": {9, 12, 15, 18},
		"P": {10}, "Q": {13}, "R": {16},
	}
	fileNodeIDs = map[string]map[string][]int64{file: {
		"run": {1}, "make": {2}, "A": {3}, "foo": {4}, "B": {5}, "bar": {6},
		"mb": {7, 11, 14, 17}, "C": {8}, "mc": {9, 12, 15, 18},
		"P": {10}, "Q": {13}, "R": {16},
	}}
	meta = map[int64]NodeMeta{
		1:  {Label: "Function", File: file, Name: "run", StartLine: 10},
		2:  {Label: "Function", File: file, Name: "make", ReturnType: "A", StartLine: 20},
		3:  {Label: cl, File: file, Name: "A", StartLine: 30},
		4:  {Label: "Method", File: file, Name: "foo", ParentID: 3, ReturnType: "B", StartLine: 31},
		5:  {Label: cl, File: file, Name: "B", StartLine: 40},
		6:  {Label: "Method", File: file, Name: "bar", ParentID: 5, ReturnType: "C", StartLine: 41},
		7:  {Label: "Method", File: file, Name: "mb", ParentID: 5, StartLine: 42},
		8:  {Label: cl, File: file, Name: "C", StartLine: 50},
		9:  {Label: "Method", File: file, Name: "mc", ParentID: 8, StartLine: 51},
		10: {Label: cl, File: file, Name: "P", StartLine: 60},
		11: {Label: "Method", File: file, Name: "mb", ParentID: 10, StartLine: 61},
		12: {Label: "Method", File: file, Name: "mc", ParentID: 10, StartLine: 62},
		13: {Label: cl, File: file, Name: "Q", StartLine: 70},
		14: {Label: "Method", File: file, Name: "mb", ParentID: 13, StartLine: 71},
		15: {Label: "Method", File: file, Name: "mc", ParentID: 13, StartLine: 72},
		16: {Label: cl, File: file, Name: "R", StartLine: 80},
		17: {Label: "Method", File: file, Name: "mb", ParentID: 16, StartLine: 81},
		18: {Label: "Method", File: file, Name: "mc", ParentID: 16, StartLine: 82},
	}
	calls = []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "mb", CalleeQualified: "b.mb", Line: 13, File: file},
		{CallerNodeIdx: 0, CalleeName: "mc", CalleeQualified: "c.mc", Line: 14, File: file},
	}
	callerIDs = []int64{1, 1}
	assignments = []parser.AssignmentRef{
		{VarName: "a", TypeName: "make", ViaReturn: true, Scope: "run", File: file, Line: 11},
		{VarName: "b", TypeName: "a.foo", ViaReturn: true, Scope: "run", File: file, Line: 12},
		{VarName: "c", TypeName: "b.bar", ViaReturn: true, Scope: "run", File: file, Line: 13},
	}
	return
}

// F4 RED→GREEN. The depth-1 receiver (b) call carries 0.9/CERTIFIED; the depth-2 receiver
// (c) call is demoted to a STRICTLY lower tier (CANDIDATE). Pre-fix both were 0.9/CERTIFIED.
func TestResolve_TypeflowFixpoint_F4_DepthDecay(t *testing.T) {
	os.Setenv("GT_TYPEFLOW_FIXPOINT", "1")
	defer os.Unsetenv("GT_TYPEFLOW_FIXPOINT")
	for _, lang := range []string{"python", "go"} {
		t.Run(lang, func(t *testing.T) {
			calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta, assignments := depthChainFixture(lang)
			reset := setB2Globals(assignments)
			defer reset()
			on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)

			d1, ok := hasEdge(on, 1, 7) // b.mb() -> B.mb (depth-1)
			if !ok || d1.Method != "type_flow" {
				t.Fatalf("F4: depth-1 b.mb() must resolve type_flow -> B.mb(7); got %+v ok=%v", d1, ok)
			}
			d2, ok := hasEdge(on, 1, 9) // c.mc() -> C.mc (depth-2)
			if !ok || d2.Method != "type_flow" {
				t.Fatalf("F4: depth-2 c.mc() must resolve type_flow -> C.mc(9); got %+v ok=%v", d2, ok)
			}
			if d1.Confidence != 0.9 || d1.TrustTier != "CERTIFIED" {
				t.Errorf("F4: depth-1 hop must keep 0.9/CERTIFIED (1.96 parity); got conf=%v tier=%q", d1.Confidence, d1.TrustTier)
			}
			if !(d2.Confidence < d1.Confidence) {
				t.Errorf("F4: depth-2 confidence (%v) must be STRICTLY below depth-1 (%v)", d2.Confidence, d1.Confidence)
			}
			if d2.TrustTier == "CERTIFIED" {
				t.Errorf("F4: depth-2 chain must be demoted BELOW CERTIFIED; got tier=%q conf=%v", d2.TrustTier, d2.Confidence)
			}
		})
	}
}

// F5 RED→GREEN. A Rust-style `::`-qualified chained call is resolved by the fixpoint too:
// the pre-pass populates the receiver var the same way, and the consumer now splits the
// call qualifier on `::` (not only `.`). Pre-fix the `.`-only split dropped `b::bar` to
// name_match; post-fix it resolves to the RIGHT target via type_flow/fixpoint.
func TestResolve_TypeflowFixpoint_F5_ColonColonReceiverConsumed(t *testing.T) {
	os.Setenv("GT_TYPEFLOW_FIXPOINT", "1")
	defer os.Unsetenv("GT_TYPEFLOW_FIXPOINT")
	calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta, assignments := chainB2Fixture("go")
	calls[0].CalleeQualified = "b::bar" // re-qualify with the Rust path separator
	reset := setB2Globals(assignments)
	defer reset()
	on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	r, ok := barEdge(on)
	if !ok {
		t.Fatalf("F5: expected an edge for b::bar(); got none (all=%+v)", on)
	}
	if r.TargetNodeID != 6 || r.Method != "type_flow" || r.EvidenceType != "fixpoint" {
		t.Errorf("F5: b::bar() must resolve to B.bar(6) via type_flow/fixpoint; got target=%d method=%q evidence=%q",
			r.TargetNodeID, r.Method, r.EvidenceType)
	}
}
