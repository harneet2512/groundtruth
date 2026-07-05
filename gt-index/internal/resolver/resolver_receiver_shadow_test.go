package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ----------------------------------------------------------------------------
// A1 + A2 (Fable 2026-07-05): receiver-blind holes where a FUNCTION-LOCAL variable
// shadows an object field / imported module of the same name. Both mint a WRONG
// high-confidence edge unless the resolver honors the local-shadows-outer scope rule
// (PyCG assignment-graph, ICSE 2021). Language-agnostic (self./this. + pkg-qualified).
// ----------------------------------------------------------------------------

// A2: self.<field>.method() must resolve against the object FIELD's type, NOT a same-named
// FUNCTION-LOCAL that shadows the field name. Before the fix the 1.96 rung stripped
// `self.client` to bare `client`, and AssignmentMap.Lookup matched the fn-local
// `client = MockClient()` FIRST → self.client.get() resolved to MockClient.get (the WRONG
// receiver). Two classes define get(); the object-field type is the discriminator.
//
// RED (revert either half — Lookup prefix-branch OR the 1.96 keep-prefix): TargetNodeID == 4
// (MockClient.get). GREEN: 3 (HttpClient.get).
func TestResolve_A2_SelfFieldBeatsLocalShadow(t *testing.T) {
	fm := BuildFileMap([]string{"svc.py", "net.py", "mock.py"}, []string{"python", "python", "python"})
	nodeIDs := map[string][]int64{
		"handler": {1}, "Service": {10},
		"HttpClient": {30}, "MockClient": {40}, "get": {3, 4},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.py":  {"handler": {1}, "Service": {10}},
		"net.py":  {"HttpClient": {30}, "get": {3}},
		"mock.py": {"MockClient": {40}, "get": {4}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", Name: "handler", File: "svc.py", ParentID: 10, StartLine: 5},
		10: {Label: "Class", Name: "Service", File: "svc.py", StartLine: 1},
		30: {Label: "Class", Name: "HttpClient", File: "net.py", StartLine: 1},
		3:  {Label: "Method", Name: "get", File: "net.py", ParentID: 30, StartLine: 2},
		40: {Label: "Class", Name: "MockClient", File: "mock.py", StartLine: 1},
		4:  {Label: "Method", Name: "get", File: "mock.py", ParentID: 40, StartLine: 2},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "self.client.get", Line: 6, File: "svc.py"},
	}
	// self.client = HttpClient() (object field, written in __init__) shadowed BY a fn-local
	// client = MockClient() in handler. The FIELD must win for self.client.get().
	assignments := []parser.AssignmentRef{
		{VarName: "self.client", TypeName: "HttpClient", Scope: "__init__", File: "svc.py"},
		{VarName: "client", TypeName: "MockClient", Scope: "handler", File: "svc.py"},
	}
	SetAssignmentIndex(BuildAssignmentIndex(assignments))
	defer SetAssignmentIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, []int64{1}, nil, fm, meta)
	var edge *ResolvedCall
	for i := range resolved {
		if resolved[i].SourceNodeID == 1 && (resolved[i].TargetNodeID == 3 || resolved[i].TargetNodeID == 4) {
			edge = &resolved[i]
		}
	}
	if edge == nil {
		t.Fatalf("self.client.get() did not resolve to any get method; resolved=%+v", resolved)
	}
	if edge.TargetNodeID != 3 {
		t.Errorf("self.client.get() resolved to id %d (%s conf %.2f); want HttpClient.get id 3 — the object FIELD type, NOT the fn-local MockClient.get id 4",
			edge.TargetNodeID, edge.Method, edge.Confidence)
	}
}

// A1: a pkg-qualified call config.get() where `config` is an imported module BUT ALSO a
// caller-scope local (`config = load_config()`) must NOT resolve to the imported module's
// get() as a CERTIFIED import edge — the local shadows the import (PyCG scope rule). Without
// the shadow the pkg-alias resolution is UNCHANGED (no over-drop — the fix is narrow).
//
// RED (revert the A1 guard): the shadowed case STILL mints Method=="import" to confmod.get(2).
func TestResolve_A1_PkgAliasShadowedByLocal(t *testing.T) {
	fm := BuildFileMap([]string{"caller.py", "confmod.py"}, []string{"python", "python"})
	nodeIDs := map[string][]int64{"handler": {1}, "get": {2}}
	fileNodeIDs := map[string]map[string][]int64{
		"caller.py":  {"handler": {1}},
		"confmod.py": {"get": {2}},
	}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "handler", File: "caller.py"},
		2: {Label: "Function", Name: "get", File: "confmod.py"},
	}
	imports := []parser.ImportRef{
		{ImportedName: "config", ModulePath: "confmod", File: "caller.py", Line: 1},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "config.get", Line: 5, File: "caller.py"},
	}
	callerIDs := []int64{1}

	// Case 1 — NO shadow: config is only the imported module → pkg-alias resolves (import fact).
	SetAssignmentIndex(nil)
	base := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	rBase, okBase := hasEdge(base, 1, 2)
	if !okBase || rBase.Method != "import" {
		t.Fatalf("no-shadow: config.get() should resolve to confmod.get(2) via the pkg-alias import; got %+v ok=%v", rBase, okBase)
	}

	// Case 2 — SHADOW: config = load_config() in handler's scope. The local shadows the
	// import; config.get() must NOT be minted as a CERTIFIED import edge.
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "config", TypeName: "LocalCfg", Scope: "handler", File: "caller.py"},
	}))
	defer SetAssignmentIndex(nil)
	shadowed := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if r, ok := hasEdge(shadowed, 1, 2); ok && r.Method == "import" {
		t.Errorf("shadowed: config.get() minted a CERTIFIED import edge to confmod.get(2) despite the caller-scope `config` shadow; got %+v — PyCG scope rule violated", r)
	}
}

// A3: a receiver TYPE NAME that matches N>1 internal classes must NOT resolve to a
// content-first pick with a cc=1 CERTIFIED stamp. Two internal classes are named Client
// (pkg_a.Client.fetch=3, pkg_b.Client.fetch=4). handler has a param `c: Client` and calls
// c.fetch() (1.94a param_type rung). Import-directed disambiguation (Strategy 1.93) picks
// the class the caller imports; with no import the name is ambiguous → ABSTAIN (never a
// cc=1 CERTIFIED launder onto an arbitrary same-named class — 2b's contract, now shared).
//
// RED (revert the A3 helper wiring → content-first pick): both cases mint a cc=1 CERTIFIED
// edge to pkg_a.Client.fetch id 3 (the content-order-first Client), ignoring the import.
func TestResolve_A3_SameNamedClassImportDisambiguates(t *testing.T) {
	fm := BuildFileMap([]string{"caller.py", "pkg_a.py", "pkg_b.py"}, []string{"python", "python", "python"})
	nodeIDs := map[string][]int64{"handler": {1}, "Client": {30, 40}, "fetch": {3, 4}}
	fileNodeIDs := map[string]map[string][]int64{
		"caller.py": {"handler": {1}},
		"pkg_a.py":  {"Client": {30}, "fetch": {3}},
		"pkg_b.py":  {"Client": {40}, "fetch": {4}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "handler", File: "caller.py"},
		30: {Label: "Class", Name: "Client", File: "pkg_a.py", StartLine: 1},
		3:  {Label: "Method", Name: "fetch", File: "pkg_a.py", ParentID: 30, StartLine: 2},
		40: {Label: "Class", Name: "Client", File: "pkg_b.py", StartLine: 1},
		4:  {Label: "Method", Name: "fetch", File: "pkg_b.py", ParentID: 40, StartLine: 2},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "fetch", CalleeQualified: "c.fetch", Line: 5, File: "caller.py"},
	}
	callerIDs := []int64{1}
	SetParamTypeIndex(map[int64]map[string]string{1: {"c": "Client"}}) // param `c: Client`
	defer SetParamTypeIndex(nil)

	certifiedTo := func(res []ResolvedCall, tgt int64) *ResolvedCall {
		for i := range res {
			if res[i].SourceNodeID == 1 && res[i].TargetNodeID == tgt &&
				res[i].CandidateCount == 1 && res[i].TrustTier == "CERTIFIED" {
				return &res[i]
			}
		}
		return nil
	}

	// Case 1 — caller imports Client from pkg_b → disambiguates to pkg_b.Client.fetch (4).
	imports := []parser.ImportRef{{ImportedName: "Client", ModulePath: "pkg_b", File: "caller.py", Line: 1}}
	got := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if certifiedTo(got, 4) == nil {
		t.Errorf("import case: c.fetch() should resolve CERTIFIED to pkg_b.Client.fetch id 4 (the imported class); got %+v", got)
	}
	if e := certifiedTo(got, 3); e != nil {
		t.Errorf("import case: minted a CERTIFIED edge to pkg_a.Client.fetch id 3 — content-first pick ignored the import; got %+v", e)
	}

	// Case 2 — NO import: type name "Client" is ambiguous with no winner → ABSTAIN (no cc=1 CERTIFIED).
	got2 := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	if e := certifiedTo(got2, 3); e != nil {
		t.Errorf("no-import case: cc=1 CERTIFIED edge to pkg_a.Client.fetch id 3 minted on an AMBIGUOUS same-named class; got %+v", e)
	}
	if e := certifiedTo(got2, 4); e != nil {
		t.Errorf("no-import case: cc=1 CERTIFIED edge to pkg_b.Client.fetch id 4 minted on an AMBIGUOUS same-named class; got %+v", e)
	}
}
