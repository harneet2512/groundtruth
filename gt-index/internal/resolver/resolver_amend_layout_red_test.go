package resolver

// RED witnesses for the batch-amend / -file id-space defect: node ids are
// AUTOINCREMENT artifacts, and the amended file's nodes re-enter at the TOP of
// the id space (the unchanged files keep theirs). Any cross-file pick that
// reads raw id order — an id sort, an insertion-ordered slice, a first map
// hit — then names a DIFFERENT logical target under an amend than under a full
// rebuild. That is the measured 49,053-vs-49,040 edge drift (±726/713 gross
// CALLS relabels) on matplotlib.
//
// Every test below builds the SAME logical graph under TWO id layouts:
//
//	rebuild layout — the edited file's nodes carry the low ids a fresh parse
//	                 assigns in walk order;
//	amend layout   — the edited file's nodes carry high ids (top of the id
//	                 space), as the amend re-insertion produces.
//
// and asserts the resolver lands on the SAME logical target — identified by
// (file, start_line), never by id — in both. Pre-fix these FAIL: the old code
// rode the id space, so the two layouts picked different classes/callees.

import (
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// layoutWitness is the logical identity of one resolved call: which mechanism
// won, and where the picked target lives — (file, start_line), the content key
// that is identical under an amend and a rebuild.
type layoutWitness struct {
	method string
	file   string
	line   int
}

// witnessCall resolves a single call against one layout and reports the
// logical target. callerID is always node 1 in these fixtures.
func witnessCall(t *testing.T, call parser.CallRef, meta map[int64]NodeMeta, nodeIDs map[string][]int64, fileNodeIDs map[string]map[string][]int64) layoutWitness {
	t.Helper()
	resolved, _ := resolveInternal([]parser.CallRef{call}, nodeIDs, fileNodeIDs, []int64{1}, nil, nil, true, meta)
	if len(resolved) != 1 {
		t.Fatalf("resolved %d calls, want exactly 1", len(resolved))
	}
	tm, ok := meta[resolved[0].TargetNodeID]
	if !ok {
		t.Fatalf("target %d has no meta — fixture bug", resolved[0].TargetNodeID)
	}
	return layoutWitness{method: resolved[0].Method, file: tm.File, line: tm.StartLine}
}

// resetStrategyIndexes clears the package-level evidence indexes so each
// subtest runs against exactly the facts it installs — the set/defer
// convention the other resolver tests already follow.
func resetStrategyIndexes(t *testing.T) {
	t.Helper()
	clear := func() {
		SetParamTypeIndex(nil)
		SetFieldTypeIndex(nil)
		SetAssignmentIndex(nil)
		SetInheritanceMap(nil)
		SetReturnShapeIndex(nil)
	}
	clear()
	t.Cleanup(clear)
}

// twoClassLayouts returns the two id-space layouts of one logical graph:
// a caller in caller.py plus two same-named classes — class `A` in a_other.py
// (the content-smaller file) and class `Z` in z_edited.py (the AMENDED file) —
// each defining `ping` at line 8. Rebuild assigns the edited file low ids;
// amend pushes it to the top of AUTOINCREMENT.
func twoClassLayouts() (rebuildMeta, amendMeta map[int64]NodeMeta, rebuildIDs, amendIDs map[string][]int64, fileNodeIDs map[string]map[string][]int64) {
	rebuildMeta = map[int64]NodeMeta{
		1: {Label: "Function", Name: "caller", File: "caller.py", StartLine: 1},
		2: {Label: "Class", Name: "Z", File: "z_edited.py", StartLine: 5},
		3: {Label: "Method", Name: "ping", File: "z_edited.py", ParentID: 2, StartLine: 8},
		4: {Label: "Class", Name: "A", File: "a_other.py", StartLine: 5},
		5: {Label: "Method", Name: "ping", File: "a_other.py", ParentID: 4, StartLine: 8},
	}
	rebuildIDs = map[string][]int64{
		"caller": {1}, "Z": {2}, "ping": {3, 5}, "A": {4},
	}
	amendMeta = map[int64]NodeMeta{
		1:  {Label: "Function", Name: "caller", File: "caller.py", StartLine: 1},
		4:  {Label: "Class", Name: "A", File: "a_other.py", StartLine: 5},
		5:  {Label: "Method", Name: "ping", File: "a_other.py", ParentID: 4, StartLine: 8},
		90: {Label: "Class", Name: "Z", File: "z_edited.py", StartLine: 5},
		91: {Label: "Method", Name: "ping", File: "z_edited.py", ParentID: 90, StartLine: 8},
	}
	amendIDs = map[string][]int64{
		"caller": {1}, "Z": {90}, "ping": {5, 91}, "A": {4},
	}
	fileNodeIDs = map[string]map[string][]int64{
		"caller.py":   {"caller": {1}},
		"a_other.py":  {"A": {4}, "ping": {5}},
		"z_edited.py": {"Z": {2}, "ping": {3}}, // rebuild layout's ids; unused below same-file checks
	}
	return
}

// Strategy 1.94 (impl_method) — the dominant measured drift site. The fallback
// class pick used to sort classIDs194 by RAW id, so an amend that renumbered
// z_edited.py's class to the top of the id space flipped the chosen method
// from a_other.py's ping to z_edited.py's ping.
func TestImplMethodPickSurvivesAmendRenumbering(t *testing.T) {
	resetStrategyIndexes(t)
	rebuildMeta, amendMeta, rebuildIDs, amendIDs, fileNodeIDs := twoClassLayouts()
	call := parser.CallRef{CalleeName: "ping", CalleeQualified: "obj.ping", File: "caller.py", Line: 3}

	gotRebuild := witnessCall(t, call, rebuildMeta, rebuildIDs, fileNodeIDs)
	gotAmend := witnessCall(t, call, amendMeta, amendIDs, fileNodeIDs)

	if gotRebuild != gotAmend {
		t.Fatalf("impl_method picked different logical targets across id layouts: "+
			"rebuild -> (%s %s:%d), amend -> (%s %s:%d)",
			gotRebuild.method, gotRebuild.file, gotRebuild.line,
			gotAmend.method, gotAmend.file, gotAmend.line)
	}
	if gotRebuild.method != "impl_method" {
		t.Fatalf("expected impl_method to fire, got %q — fixture drifted off the tested rung", gotRebuild.method)
	}
	// The content-smallest class (a_other.py) must own the pick in BOTH layouts.
	if gotRebuild.file != "a_other.py" || gotRebuild.line != 8 {
		t.Fatalf("impl_method picked %s:%d, want a_other.py:8 (content-smallest class)",
			gotRebuild.file, gotRebuild.line)
	}
}

// Strategy 1.94a (declared param type): `obj` is a declared parameter of type
// T; two files declare class T. The class loop iterated nodeIDs["T"] in
// slice order — insertion order, which an amend turns into id-space order.
func TestDeclaredTypePickSurvivesAmendRenumbering(t *testing.T) {
	resetStrategyIndexes(t)
	SetParamTypeIndex(map[int64]map[string]string{1: {"obj": "T"}})

	// Same two-layout shape, but both classes are named T (the param type).
	rebuildMeta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "caller", File: "caller.py", StartLine: 1},
		2: {Label: "Class", Name: "T", File: "z_edited.py", StartLine: 5},
		3: {Label: "Method", Name: "ping", File: "z_edited.py", ParentID: 2, StartLine: 8},
		4: {Label: "Class", Name: "T", File: "a_other.py", StartLine: 5},
		5: {Label: "Method", Name: "ping", File: "a_other.py", ParentID: 4, StartLine: 8},
	}
	rebuildIDs := map[string][]int64{"caller": {1}, "T": {2, 4}, "ping": {3, 5}}
	amendMeta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "caller", File: "caller.py", StartLine: 1},
		4:  {Label: "Class", Name: "T", File: "a_other.py", StartLine: 5},
		5:  {Label: "Method", Name: "ping", File: "a_other.py", ParentID: 4, StartLine: 8},
		90: {Label: "Class", Name: "T", File: "z_edited.py", StartLine: 5},
		91: {Label: "Method", Name: "ping", File: "z_edited.py", ParentID: 90, StartLine: 8},
	}
	amendIDs := map[string][]int64{"caller": {1}, "T": {4, 90}, "ping": {5, 91}}
	fileNodeIDs := map[string]map[string][]int64{"caller.py": {"caller": {1}}}
	call := parser.CallRef{CalleeName: "ping", CalleeQualified: "obj.ping", File: "caller.py", Line: 3}

	gotRebuild := witnessCall(t, call, rebuildMeta, rebuildIDs, fileNodeIDs)
	gotAmend := witnessCall(t, call, amendMeta, amendIDs, fileNodeIDs)

	if gotRebuild != gotAmend {
		t.Fatalf("param_type pick differs across id layouts: rebuild -> %s:%d, amend -> %s:%d",
			gotRebuild.file, gotRebuild.line, gotAmend.file, gotAmend.line)
	}
	if gotRebuild.method != "type_flow" || gotRebuild.file != "a_other.py" {
		t.Fatalf("want type_flow -> a_other.py:8, got %s -> %s:%d",
			gotRebuild.method, gotRebuild.file, gotRebuild.line)
	}
}

// Strategy 1.95 (type_flow on a class-qualified call `T.ping()`): the first
// same-named class holding the method won; the slice is insertion/id order.
func TestTypeFlowPickSurvivesAmendRenumbering(t *testing.T) {
	resetStrategyIndexes(t)

	rebuildMeta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "caller", File: "caller.py", StartLine: 1},
		2: {Label: "Class", Name: "T", File: "z_edited.py", StartLine: 5},
		3: {Label: "Method", Name: "ping", File: "z_edited.py", ParentID: 2, StartLine: 8},
		4: {Label: "Class", Name: "T", File: "a_other.py", StartLine: 5},
		5: {Label: "Method", Name: "ping", File: "a_other.py", ParentID: 4, StartLine: 8},
	}
	rebuildIDs := map[string][]int64{"caller": {1}, "T": {2, 4}, "ping": {3, 5}}
	amendMeta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "caller", File: "caller.py", StartLine: 1},
		4:  {Label: "Class", Name: "T", File: "a_other.py", StartLine: 5},
		5:  {Label: "Method", Name: "ping", File: "a_other.py", ParentID: 4, StartLine: 8},
		90: {Label: "Class", Name: "T", File: "z_edited.py", StartLine: 5},
		91: {Label: "Method", Name: "ping", File: "z_edited.py", ParentID: 90, StartLine: 8},
	}
	amendIDs := map[string][]int64{"caller": {1}, "T": {4, 90}, "ping": {5, 91}}
	fileNodeIDs := map[string]map[string][]int64{"caller.py": {"caller": {1}}}
	call := parser.CallRef{CalleeName: "ping", CalleeQualified: "T.ping", File: "caller.py", Line: 3}

	gotRebuild := witnessCall(t, call, rebuildMeta, rebuildIDs, fileNodeIDs)
	gotAmend := witnessCall(t, call, amendMeta, amendIDs, fileNodeIDs)

	if gotRebuild != gotAmend {
		t.Fatalf("type_flow class pick differs across id layouts: rebuild -> %s:%d, amend -> %s:%d",
			gotRebuild.file, gotRebuild.line, gotAmend.file, gotAmend.line)
	}
	if gotRebuild.method != "type_flow" || gotRebuild.file != "a_other.py" {
		t.Fatalf("want type_flow -> a_other.py:8, got %s -> %s:%d",
			gotRebuild.method, gotRebuild.file, gotRebuild.line)
	}
}

// Strategy 1.96 (assignment flow `y = T(); y.ping()`): four classes own `ping`
// so 1.94 abstains; the assignment narrows the receiver to class name T, which
// exists in two files — the first-in-slice class won.
func TestAssignmentFlowPickSurvivesAmendRenumbering(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "y", TypeName: "T", Scope: "caller", File: "caller.py", Line: 1},
	}))

	// T lives in a_other.py AND z_edited.py; W and X pad methodClassCount["ping"]
	// to 4 so Strategy 1.94 (<=3 implementors) abstains and 1.96 runs.
	rebuildMeta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "caller", File: "caller.py", StartLine: 1},
		2:  {Label: "Class", Name: "T", File: "z_edited.py", StartLine: 5},
		3:  {Label: "Method", Name: "ping", File: "z_edited.py", ParentID: 2, StartLine: 8},
		4:  {Label: "Class", Name: "T", File: "a_other.py", StartLine: 5},
		5:  {Label: "Method", Name: "ping", File: "a_other.py", ParentID: 4, StartLine: 8},
		6:  {Label: "Class", Name: "W", File: "b_third.py", StartLine: 5},
		7:  {Label: "Method", Name: "ping", File: "b_third.py", ParentID: 6, StartLine: 8},
		8:  {Label: "Class", Name: "X", File: "c_fourth.py", StartLine: 5},
		9:  {Label: "Method", Name: "ping", File: "c_fourth.py", ParentID: 8, StartLine: 8},
		10: {Label: "Class", Name: "Y", File: "d_fifth.py", StartLine: 5},
		11: {Label: "Method", Name: "ping", File: "d_fifth.py", ParentID: 10, StartLine: 8},
	}
	rebuildIDs := map[string][]int64{
		"caller": {1}, "T": {2, 4}, "ping": {3, 5, 7, 9, 11}, "W": {6}, "X": {8}, "Y": {10},
	}
	amendMeta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "caller", File: "caller.py", StartLine: 1},
		4:  {Label: "Class", Name: "T", File: "a_other.py", StartLine: 5},
		5:  {Label: "Method", Name: "ping", File: "a_other.py", ParentID: 4, StartLine: 8},
		6:  {Label: "Class", Name: "W", File: "b_third.py", StartLine: 5},
		7:  {Label: "Method", Name: "ping", File: "b_third.py", ParentID: 6, StartLine: 8},
		8:  {Label: "Class", Name: "X", File: "c_fourth.py", StartLine: 5},
		9:  {Label: "Method", Name: "ping", File: "c_fourth.py", ParentID: 8, StartLine: 8},
		10: {Label: "Class", Name: "Y", File: "d_fifth.py", StartLine: 5},
		11: {Label: "Method", Name: "ping", File: "d_fifth.py", ParentID: 10, StartLine: 8},
		90: {Label: "Class", Name: "T", File: "z_edited.py", StartLine: 5},
		91: {Label: "Method", Name: "ping", File: "z_edited.py", ParentID: 90, StartLine: 8},
	}
	amendIDs := map[string][]int64{
		"caller": {1}, "T": {4, 90}, "ping": {5, 7, 9, 11, 91}, "W": {6}, "X": {8}, "Y": {10},
	}
	fileNodeIDs := map[string]map[string][]int64{"caller.py": {"caller": {1}}}
	call := parser.CallRef{CalleeName: "ping", CalleeQualified: "y.ping", File: "caller.py", Line: 3}

	gotRebuild := witnessCall(t, call, rebuildMeta, rebuildIDs, fileNodeIDs)
	gotAmend := witnessCall(t, call, amendMeta, amendIDs, fileNodeIDs)

	if gotRebuild != gotAmend {
		t.Fatalf("assignment-flow class pick differs across id layouts: rebuild -> %s:%d, amend -> %s:%d",
			gotRebuild.file, gotRebuild.line, gotAmend.file, gotAmend.line)
	}
	if gotRebuild.method != "type_flow" || gotRebuild.file != "a_other.py" {
		t.Fatalf("want type_flow -> a_other.py:8, got %s -> %s:%d",
			gotRebuild.method, gotRebuild.file, gotRebuild.line)
	}
}

// Strategy 1.97 (return-type bridging `mk().ping()`): TWO picks rode the id
// space — which same-named factory supplies the return type, and which
// same-named class owns the method. Here the factory pick is exercised: mk in
// z_edited.py returns T, mk in a_other.py returns V; both T and V (plus W, X)
// define ping so 1.94 abstains.
func TestReturnTypeBridgePickSurvivesAmendRenumbering(t *testing.T) {
	resetStrategyIndexes(t)

	rebuildMeta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "caller", File: "caller.py", StartLine: 1},
		2:  {Label: "Function", Name: "mk", File: "z_edited.py", StartLine: 5, ReturnType: "T"},
		3:  {Label: "Function", Name: "mk", File: "a_other.py", StartLine: 5, ReturnType: "V"},
		4:  {Label: "Class", Name: "T", File: "t_file.py", StartLine: 5},
		5:  {Label: "Method", Name: "ping", File: "t_file.py", ParentID: 4, StartLine: 8},
		6:  {Label: "Class", Name: "V", File: "v_file.py", StartLine: 5},
		7:  {Label: "Method", Name: "ping", File: "v_file.py", ParentID: 6, StartLine: 8},
		8:  {Label: "Class", Name: "W", File: "b_third.py", StartLine: 5},
		9:  {Label: "Method", Name: "ping", File: "b_third.py", ParentID: 8, StartLine: 8},
		10: {Label: "Class", Name: "X", File: "c_fourth.py", StartLine: 5},
		11: {Label: "Method", Name: "ping", File: "c_fourth.py", ParentID: 10, StartLine: 8},
	}
	// Rebuild slice order: the edited file's factory comes first (low id).
	rebuildIDs := map[string][]int64{
		"caller": {1}, "mk": {2, 3}, "T": {4}, "ping": {5, 7, 9, 11}, "V": {6}, "W": {8}, "X": {10},
	}
	amendMeta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "caller", File: "caller.py", StartLine: 1},
		3:  {Label: "Function", Name: "mk", File: "a_other.py", StartLine: 5, ReturnType: "V"},
		4:  {Label: "Class", Name: "T", File: "t_file.py", StartLine: 5},
		5:  {Label: "Method", Name: "ping", File: "t_file.py", ParentID: 4, StartLine: 8},
		6:  {Label: "Class", Name: "V", File: "v_file.py", StartLine: 5},
		7:  {Label: "Method", Name: "ping", File: "v_file.py", ParentID: 6, StartLine: 8},
		8:  {Label: "Class", Name: "W", File: "b_third.py", StartLine: 5},
		9:  {Label: "Method", Name: "ping", File: "b_third.py", ParentID: 8, StartLine: 8},
		10: {Label: "Class", Name: "X", File: "c_fourth.py", StartLine: 5},
		11: {Label: "Method", Name: "ping", File: "c_fourth.py", ParentID: 10, StartLine: 8},
		90: {Label: "Function", Name: "mk", File: "z_edited.py", StartLine: 5, ReturnType: "T"},
	}
	// Amend slice order: the edited file's factory sits at the top of the id
	// space — after every retained node.
	amendIDs := map[string][]int64{
		"caller": {1}, "mk": {3, 90}, "T": {4}, "ping": {5, 7, 9, 11}, "V": {6}, "W": {8}, "X": {10},
	}
	fileNodeIDs := map[string]map[string][]int64{"caller.py": {"caller": {1}}}
	call := parser.CallRef{CalleeName: "ping", CalleeQualified: "mk().ping", File: "caller.py", Line: 3}

	gotRebuild := witnessCall(t, call, rebuildMeta, rebuildIDs, fileNodeIDs)
	gotAmend := witnessCall(t, call, amendMeta, amendIDs, fileNodeIDs)

	if gotRebuild != gotAmend {
		t.Fatalf("return_type pick differs across id layouts: rebuild -> (%s %s:%d), amend -> (%s %s:%d)",
			gotRebuild.method, gotRebuild.file, gotRebuild.line,
			gotAmend.method, gotAmend.file, gotAmend.line)
	}
	// Content order puts a_other.py's factory first → it returns V → V.ping.
	if gotRebuild.method != "return_type" || gotRebuild.file != "v_file.py" {
		t.Fatalf("want return_type -> v_file.py:8, got %s -> %s:%d",
			gotRebuild.method, gotRebuild.file, gotRebuild.line)
	}
}

// The class-level pick inside 1.97: one factory returning T, two same-named
// classes T in different files. (The factory pick above proved the funcIDs
// loop; this pins the classIDs loop on the same rung.)
func TestReturnTypeClassPickSurvivesAmendRenumbering(t *testing.T) {
	resetStrategyIndexes(t)

	rebuildMeta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "caller", File: "caller.py", StartLine: 1},
		2:  {Label: "Class", Name: "T", File: "z_edited.py", StartLine: 5},
		3:  {Label: "Method", Name: "ping", File: "z_edited.py", ParentID: 2, StartLine: 8},
		4:  {Label: "Class", Name: "T", File: "a_other.py", StartLine: 5},
		5:  {Label: "Method", Name: "ping", File: "a_other.py", ParentID: 4, StartLine: 8},
		6:  {Label: "Function", Name: "mk", File: "f_file.py", StartLine: 5, ReturnType: "T"},
		7:  {Label: "Class", Name: "W", File: "b_third.py", StartLine: 5},
		8:  {Label: "Method", Name: "ping", File: "b_third.py", ParentID: 7, StartLine: 8},
		9:  {Label: "Class", Name: "X", File: "c_fourth.py", StartLine: 5},
		10: {Label: "Method", Name: "ping", File: "c_fourth.py", ParentID: 9, StartLine: 8},
		11: {Label: "Class", Name: "Y", File: "d_fifth.py", StartLine: 5},
		12: {Label: "Method", Name: "ping", File: "d_fifth.py", ParentID: 11, StartLine: 8},
	}
	rebuildIDs := map[string][]int64{
		"caller": {1}, "T": {2, 4}, "ping": {3, 5, 8, 10, 12}, "mk": {6}, "W": {7}, "X": {9}, "Y": {11},
	}
	amendMeta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "caller", File: "caller.py", StartLine: 1},
		4:  {Label: "Class", Name: "T", File: "a_other.py", StartLine: 5},
		5:  {Label: "Method", Name: "ping", File: "a_other.py", ParentID: 4, StartLine: 8},
		6:  {Label: "Function", Name: "mk", File: "f_file.py", StartLine: 5, ReturnType: "T"},
		7:  {Label: "Class", Name: "W", File: "b_third.py", StartLine: 5},
		8:  {Label: "Method", Name: "ping", File: "b_third.py", ParentID: 7, StartLine: 8},
		9:  {Label: "Class", Name: "X", File: "c_fourth.py", StartLine: 5},
		10: {Label: "Method", Name: "ping", File: "c_fourth.py", ParentID: 9, StartLine: 8},
		11: {Label: "Class", Name: "Y", File: "d_fifth.py", StartLine: 5},
		12: {Label: "Method", Name: "ping", File: "d_fifth.py", ParentID: 11, StartLine: 8},
		90: {Label: "Class", Name: "T", File: "z_edited.py", StartLine: 5},
		91: {Label: "Method", Name: "ping", File: "z_edited.py", ParentID: 90, StartLine: 8},
	}
	amendIDs := map[string][]int64{
		"caller": {1}, "T": {4, 90}, "ping": {5, 8, 10, 12, 91}, "mk": {6}, "W": {7}, "X": {9}, "Y": {11},
	}
	fileNodeIDs := map[string]map[string][]int64{"caller.py": {"caller": {1}}}
	call := parser.CallRef{CalleeName: "ping", CalleeQualified: "mk().ping", File: "caller.py", Line: 3}

	gotRebuild := witnessCall(t, call, rebuildMeta, rebuildIDs, fileNodeIDs)
	gotAmend := witnessCall(t, call, amendMeta, amendIDs, fileNodeIDs)

	if gotRebuild != gotAmend {
		t.Fatalf("return_type class pick differs across id layouts: rebuild -> %s:%d, amend -> %s:%d",
			gotRebuild.file, gotRebuild.line, gotAmend.file, gotAmend.line)
	}
	if gotRebuild.method != "return_type" || gotRebuild.file != "a_other.py" {
		t.Fatalf("want return_type -> a_other.py:8, got %s -> %s:%d",
			gotRebuild.method, gotRebuild.file, gotRebuild.line)
	}
}

// relationships.go: the EXTENDS/IMPLEMENTS/COMPOSES class+interface fallback
// used to return entries[0] — the first row of an UNORDERED index scan whose
// position tracks the id space a batch amend renumbers.
func TestRelationshipFallbackPicksAreLayoutInvariant(t *testing.T) {
	// Same logical index, two entry arrangements: rebuild order (edited file's
	// class at low id) and amend order (same class renumbered to the top of the
	// id space and listed last).
	rebuildIndex := map[string][]classNodeEntry{
		"Widget": {
			{Name: "Widget", FilePath: "z_edited.py", Line: 5, ID: 2},
			{Name: "Widget", FilePath: "a_other.py", Line: 5, ID: 4},
		},
	}
	amendIndex := map[string][]classNodeEntry{
		"Widget": {
			{Name: "Widget", FilePath: "a_other.py", Line: 5, ID: 4},
			{Name: "Widget", FilePath: "z_edited.py", Line: 5, ID: 90},
		},
	}

	gotRebuild := resolveClassNode("Widget", "use.py", rebuildIndex)
	gotAmend := resolveClassNode("Widget", "use.py", amendIndex)
	if gotRebuild != 4 || gotAmend != 4 {
		t.Fatalf("resolveClassNode cross-file fallback picked ids %d (rebuild) / %d (amend), "+
			"want the content-smallest class (a_other.py) in BOTH — 4", gotRebuild, gotAmend)
	}
	if got := resolveInterfaceNode("Widget", "use.py", rebuildIndex); got != 4 {
		t.Fatalf("resolveInterfaceNode rebuild picked %d, want 4 (a_other.py)", got)
	}
	if got := resolveInterfaceNode("Widget", "use.py", amendIndex); got != 4 {
		t.Fatalf("resolveInterfaceNode amend picked %d, want 4 (a_other.py)", got)
	}

	// Same-file preference still wins over the content-min cross-file entry:
	// a use.py Widget at a LATER line than the cross-file one must still win.
	withLocal := map[string][]classNodeEntry{
		"Widget": {
			{Name: "Widget", FilePath: "a_other.py", Line: 5, ID: 4},
			{Name: "Widget", FilePath: "use.py", Line: 30, ID: 9},
		},
	}
	if got := resolveClassNode("Widget", "use.py", withLocal); got != 9 {
		t.Fatalf("same-file preference lost: picked %d, want 9 (use.py)", got)
	}
	if got := resolveClassNodeSameFileOrUnique("Widget", "use.py", withLocal); got != 9 {
		t.Fatalf("sameFileOrUnique same-file pick = %d, want 9 (use.py)", got)
	}
	// Two same-named Widgets in use.py: the earliest declaration (smallest
	// start_line) wins, not the first scan row.
	twoLocal := map[string][]classNodeEntry{
		"Widget": {
			{Name: "Widget", FilePath: "use.py", Line: 40, ID: 9},
			{Name: "Widget", FilePath: "use.py", Line: 12, ID: 7},
		},
	}
	if got := resolveClassNode("Widget", "use.py", twoLocal); got != 7 {
		t.Fatalf("two same-file Widgets: picked %d, want 7 (line 12 first)", got)
	}
	// Globally-unique cross-file still resolves.
	unique := map[string][]classNodeEntry{
		"Widget": {{Name: "Widget", FilePath: "a_other.py", Line: 5, ID: 4}},
	}
	if got := resolveClassNodeSameFileOrUnique("Widget", "use.py", unique); got != 4 {
		t.Fatalf("unique cross-file pick = %d, want 4", got)
	}
	// Cross-file ambiguous still abstains.
	if got := resolveClassNodeSameFileOrUnique("Widget", "use.py", rebuildIndex); got != 0 {
		t.Fatalf("ambiguous cross-file pick = %d, want abstain (0)", got)
	}
}

// promote.go classByName → PRECEDES receiver-type gating. A `self.f` receiver
// typed `f: T` resolves T through classByName — first-writer on the id-ordered
// scan — and resolveClassMethod then requires the ordered methods to be
// SAME-FILE members of that class. When the same-named class in the edited
// file carried the low ids (rebuild), first-writer named it; after an amend
// pushed it to the top of the id space, first-writer named the caller.py
// class instead — the PRECEDES edge existed in only ONE layout (the
// 49,053-vs-49,040 count-diff class). Content-smallest (file,line,id) names
// the caller.py class in BOTH.
func TestPromoteClassByNamePickIsLayoutInvariant(t *testing.T) {
	// two sets of explicit ids for the same logical nodes.
	layouts := []struct {
		name  string
		nodes string
	}{
		{"rebuild", `INSERT INTO nodes (id,label,name,file_path,start_line,signature,language,parent_id) VALUES
		  (1,'Class','C','caller.py',1,'','python',0),
		  (2,'Method','runner','caller.py',2,'def runner(self)','python',1),
		  (3,'Class','T','z_edited.py',5,'','python',0),
		  (4,'Method','alpha','z_edited.py',6,'','python',3),
		  (5,'Method','beta','z_edited.py',7,'','python',3),
		  (10,'Class','T','caller.py',10,'','python',0),
		  (11,'Method','alpha','caller.py',11,'','python',10),
		  (12,'Method','beta','caller.py',12,'','python',10)`},
		{"amend", `INSERT INTO nodes (id,label,name,file_path,start_line,signature,language,parent_id) VALUES
		  (1,'Class','C','caller.py',1,'','python',0),
		  (2,'Method','runner','caller.py',2,'def runner(self)','python',1),
		  (10,'Class','T','caller.py',10,'','python',0),
		  (11,'Method','alpha','caller.py',11,'','python',10),
		  (12,'Method','beta','caller.py',12,'','python',10),
		  (90,'Class','T','z_edited.py',5,'','python',0),
		  (91,'Method','alpha','z_edited.py',6,'','python',90),
		  (92,'Method','beta','z_edited.py',7,'','python',90)`},
	}

	for _, layout := range layouts {
		t.Run(layout.name, func(t *testing.T) {
			root := t.TempDir()
			db, err := store.Open(filepath.Join(root, "graph.db"))
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			execSQL(t, db, layout.nodes)
			// C declares field f: T; runner records `self.f: alpha -> beta`.
			execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
			  (1,'class_field','f: T',2,1.0),
			  (2,'call_order','self.f: alpha -> beta',3,0.6)`)

			if _, err := PromotePropertyEdges(db); err != nil {
				t.Fatal(err)
			}
			// The caller.py T (id 10) owns alpha(11)/beta(12) → PRECEDES 11->12.
			if n := countEdges(t, db, `type='PRECEDES'`); n != 1 {
				t.Fatalf("%s layout: %d PRECEDES edges, want 1 — classByName picked "+
					"the z_edited.py class (its methods are not same-file, so the "+
					"edge silently vanished)", layout.name, n)
			}
			if !edgeExists(t, db, "PRECEDES", 11, 12) {
				t.Fatalf("%s layout: PRECEDES did not land on caller.py's alpha->beta (11->12)", layout.name)
			}
		})
	}
}

// promoteSerde's cross-file partner fallback ranged idx.fnl (a map) and broke
// on the first hit — run-random — over ids that renumber under an amend. The
// helper must name the content-smallest (file,id) partner every run, in every
// layout.
func TestSerdeCrossFilePartnerPickIsLayoutInvariant(t *testing.T) {
	build := func(zID, aID int64) *promoteIndexes {
		return &promoteIndexes{
			fnl: map[fnlKey]int64{
				{file: "a_other.py", name: "dump", line: 5}:  aID,
				{file: "z_edited.py", name: "dump", line: 5}: zID,
				{file: "z_edited.py", name: "load", line: 9}: zID + 1,
			},
		}
	}
	rebuild := build(3, 40) // edited file's dump carries the low id
	amend := build(90, 40)  // …and the high id after an amend

	for i := 0; i < 256; i++ { // map order is randomized — run enough to catch it
		if got := rebuild.fnlAnyFile("dump", 5); got != 40 {
			t.Fatalf("rebuild layout: fnlAnyFile(dump,5) = %d, want 40 (a_other.py)", got)
		}
		if got := amend.fnlAnyFile("dump", 5); got != 40 {
			t.Fatalf("amend layout: fnlAnyFile(dump,5) = %d, want 40 (a_other.py)", got)
		}
	}
	if got := rebuild.fnlAnyFile("absent", 5); got != 0 {
		t.Fatalf("unmatched partner resolved to %d, want 0 (stays a property)", got)
	}
}

// mro.go: an equal-coverage tie used to keep the lowest-id class's
// linearisation. With the candidate ORDER published in the callsite trace,
// an amend that renumbered the edited file's class flipped which
// linearisation ordered the set. Content order keeps the same winner.
func TestMROEqualCoverageTieBreakIsLayoutInvariant(t *testing.T) {
	// Two unrelated hierarchies — candidates mA (class A, a_other.py) and
	// mZ (class Z, z_edited.py). Each class's linearisation covers exactly one
	// candidate class → a coverage tie → the tie-break decides whose order runs.
	// The edited file is a_other.py here: rebuild gives its class id 2, amend 90.
	rebuild := map[int64]NodeMeta{
		20: {Name: "m", File: "a_other.py", ParentID: 2, StartLine: 8},
		30: {Name: "m", File: "z_edited.py", ParentID: 3, StartLine: 8},
		2:  {Name: "A", Label: "Class", File: "a_other.py", StartLine: 5},
		3:  {Name: "Z", Label: "Class", File: "z_edited.py", StartLine: 5},
	}
	rebuildBases := map[int64][]int64{2: {}, 3: {}}
	amend := map[int64]NodeMeta{
		20: {Name: "m", File: "a_other.py", ParentID: 90, StartLine: 8},
		30: {Name: "m", File: "z_edited.py", ParentID: 3, StartLine: 8},
		90: {Name: "A", Label: "Class", File: "a_other.py", StartLine: 5},
		3:  {Name: "Z", Label: "Class", File: "z_edited.py", StartLine: 5},
	}
	amendBases := map[int64][]int64{90: {}, 3: {}}

	candidates := []int64{20, 30}
	gotRebuild := OrderCandidatesByMRO("python", "inherited", candidates, rebuildBases, rebuild)
	gotAmend := OrderCandidatesByMRO("python", "inherited", candidates, amendBases, amend)
	if !equalMROIDs(gotRebuild.Order, gotAmend.Order) {
		t.Fatalf("MRO tie-break picked different linearisations across id layouts: "+
			"rebuild order %v, amend order %v", gotRebuild.Order, gotAmend.Order)
	}
	// a_other.py is content-first → class A's linearisation orders candidate
	// 20 (its member) ahead of candidate 30 in BOTH layouts.
	if want := []int64{20, 30}; !equalMROIDs(gotRebuild.Order, want) {
		t.Fatalf("MRO order %v, want %v (a_other.py's member first)", gotRebuild.Order, want)
	}
}
