package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// TestBuildReturnShapeIndex_ConstructorOnly locks the GAP C correct-or-quiet contract:
// the index records a constructed type ONLY for a CLEAN single-constructor return whose
// name is an internal class-like node, across the Python/JS (`Name(...)`) and Go/Rust
// (`&Struct{...}` / `Struct{...}`) idioms — and abstains on everything else.
func TestBuildReturnShapeIndex_ConstructorOnly(t *testing.T) {
	// nodeDBIDs: NodeIdx i -> dbid (i+1)*10. Funcs are NodeIdx 0..; only the constructor
	// returns that name an internal class should produce an entry.
	nodeDBIDs := []int64{10, 20, 30, 40, 50, 60, 70, 80, 90}
	classNames := map[string]bool{
		"User": true, "Service": true, "Stack": true, "Account": true,
	}
	props := []parser.PropertyRef{
		// 0 -> Python/JS constructor: ACCEPT (User is internal)
		{NodeIdx: 0, Kind: "return_shape", Value: "value|User(name, email)"},
		// 1 -> Go composite literal &Struct{}: ACCEPT
		{NodeIdx: 1, Kind: "return_shape", Value: "value|&Service{client: c}"},
		// 2 -> Go composite literal without &: ACCEPT
		{NodeIdx: 2, Kind: "return_shape", Value: "value|Stack{items: nil}"},
		// 3 -> constructor of a NON-internal type: ABSTAIN
		{NodeIdx: 3, Kind: "return_shape", Value: "value|HttpResponse(200)"},
		// 4 -> collection literal (NOT a struct constructor): ABSTAIN
		{NodeIdx: 4, Kind: "return_shape", Value: "collection|[User(a), User(b)]"},
		// 5 -> none: ABSTAIN
		{NodeIdx: 5, Kind: "return_shape", Value: "none"},
		// 6 -> non-constructor value (a bare var): ABSTAIN
		{NodeIdx: 6, Kind: "return_shape", Value: "value|self.account"},
		// 7 -> AMBIGUOUS: two different internal constructors -> DROP (record nothing)
		{NodeIdx: 7, Kind: "return_shape", Value: "value|User(a)"},
		{NodeIdx: 7, Kind: "return_shape", Value: "value|Account(b)"},
		// 8 -> method chain (ends in `)` but is NOT a bare constructor): ABSTAIN
		{NodeIdx: 8, Kind: "return_shape", Value: "value|User(a).clone()"},
	}
	idx := BuildReturnShapeIndex(props, nodeDBIDs, classNames)

	if got := idx[10]; got != "User" {
		t.Errorf("Name(...) python/js constructor: got %q, want User", got)
	}
	if got := idx[20]; got != "Service" {
		t.Errorf("&Struct{...} go literal: got %q, want Service", got)
	}
	if got := idx[30]; got != "Stack" {
		t.Errorf("Struct{...} go literal: got %q, want Stack", got)
	}
	if got, ok := idx[40]; ok {
		t.Errorf("non-internal constructor must abstain, got %q", got)
	}
	if got, ok := idx[50]; ok {
		t.Errorf("collection literal must abstain, got %q", got)
	}
	if got, ok := idx[60]; ok {
		t.Errorf("none-return must abstain, got %q", got)
	}
	if got, ok := idx[70]; ok {
		t.Errorf("bare-var value must abstain, got %q", got)
	}
	if got, ok := idx[80]; ok {
		t.Errorf("ambiguous two-constructor func must DROP, got %q", got)
	}
	if got, ok := idx[90]; ok {
		t.Errorf("method-chain (not a bare constructor) must abstain, got %q", got)
	}
}

// TestResolve_ReturnShapeFallback_ResolverPath proves the return-type bridge (Strategy
// 1.97) resolves factory().method() via the CONSTRUCTOR return shape when the parser
// captured NO declared return type — the GAP C end-to-end behavior on the resolver path.
// Mirrors TestResolve_FieldType_ResolverPath: it injects the index directly (does not
// exercise BuildReturnShapeIndex, covered above) and asserts the resolved edge.
func TestResolve_ReturnShapeFallback_ResolverPath(t *testing.T) {
	// Graph: makeUser() (no declared return type) returns User{}; Save() is defined on
	// User AND on 3 OTHER classes (4 total) so neither unique-method (1.98) nor
	// few-implementor (1.94, cap=3 classes) can fire — the ONLY rung that can resolve
	// makeUser().Save() to User.Save is the return-type bridge reading the constructor
	// return shape. Call site: makeUser().Save().
	fileMap := BuildFileMap([]string{"app.go"}, []string{"go"})
	nodeIDs := map[string][]int64{
		"makeUser": {1}, "User": {2}, "Save": {3, 6, 8, 10}, "caller": {4},
		"Order": {5}, "Cart": {7}, "Item": {9},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"app.go": {
			"makeUser": {1}, "User": {2}, "Save": {3, 6, 8, 10}, "caller": {4},
			"Order": {5}, "Cart": {7}, "Item": {9},
		},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", File: "app.go", Name: "makeUser", ReturnType: ""}, // NO declared return
		2:  {Label: "Class", File: "app.go", Name: "User"},
		3:  {Label: "Method", File: "app.go", Name: "Save", ParentID: 2}, // User.Save (the target)
		4:  {Label: "Function", File: "app.go", Name: "caller"},
		5:  {Label: "Class", File: "app.go", Name: "Order"},
		6:  {Label: "Method", File: "app.go", Name: "Save", ParentID: 5},
		7:  {Label: "Class", File: "app.go", Name: "Cart"},
		8:  {Label: "Method", File: "app.go", Name: "Save", ParentID: 7},
		9:  {Label: "Class", File: "app.go", Name: "Item"},
		10: {Label: "Method", File: "app.go", Name: "Save", ParentID: 9}, // 4 classes -> 1.94 abstains
	}

	// Clear any cross-test global index state so ONLY the return-shape bridge is in play.
	SetAssignmentIndex(nil)
	SetParamTypeIndex(nil)
	SetFieldTypeIndex(nil)
	SetInheritanceMap(nil)
	calls := []parser.CallRef{
		{CalleeName: "Save", CalleeQualified: "makeUser.Save", File: "app.go", Line: 10},
	}
	callerIDs := []int64{4}

	// RED: without the return-shape index, NO return-type-bridge edge to User.Save fires.
	SetReturnShapeIndex(nil)
	got := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fileMap, meta)
	if m := bridgeMethodTo(got, 4, 3); m != "" {
		t.Fatalf("RED expectation: without return_shape index, the return-type bridge must NOT resolve makeUser().Save() to User.Save; got method %q", m)
	}

	// GREEN: with the constructor-return-shape index populated, the return-type bridge
	// (Strategy 1.97) resolves makeUser().Save() to User.Save via the constructor fact.
	SetReturnShapeIndex(map[int64]string{1: "User"})
	defer SetReturnShapeIndex(nil)
	got = Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fileMap, meta)
	if m := bridgeMethodTo(got, 4, 3); m != "return_type" && m != "type_flow" {
		t.Errorf("GREEN expectation: makeUser().Save() must resolve to User.Save via the return-type bridge (return_type/type_flow); got method %q (all=%+v)", m, got)
	}
}

// bridgeMethodTo returns the resolution Method of a (src->tgt) edge produced by a
// return-type/type-flow bridge, or "" if no such bridge edge exists. Other strategies
// (impl_method, name_match, unique_method) are NOT counted — only the return-type bridges
// prove the GAP C fallback fired.
func bridgeMethodTo(rcs []ResolvedCall, src, tgt int64) string {
	for _, rc := range rcs {
		if rc.SourceNodeID == src && rc.TargetNodeID == tgt &&
			(rc.Method == "return_type" || rc.Method == "type_flow") {
			return rc.Method
		}
	}
	return ""
}
