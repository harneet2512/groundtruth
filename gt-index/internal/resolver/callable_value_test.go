package resolver

// End-to-end tests for the callable-value rung (Strategy 1.96b + the
// dispatch_form "function_value" early gate): bare/qualified/field aliases and
// argument→formal parameter flow resolve to the bound callable with
// resolution_method "callable_value"; ambiguous or unprovable bindings abstain.

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

func resolveCalls(t *testing.T, calls []parser.CallRef, callerIDs []int64, meta map[int64]NodeMeta, nodeIDs map[string][]int64, fileNodeIDs map[string]map[string][]int64, imports []parser.ImportRef, fileMap map[string][]string) []ResolvedCall {
	t.Helper()
	resolved, _ := resolveInternal(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fileMap, true, meta)
	return resolved
}

func edgeFor(t *testing.T, resolved []ResolvedCall, ordinal int) ResolvedCall {
	t.Helper()
	var found []ResolvedCall
	for _, rc := range resolved {
		if rc.CallsiteOrdinal == ordinal {
			found = append(found, rc)
		}
	}
	if len(found) != 1 {
		t.Fatalf("callsite %d produced %d edges, want exactly 1: %+v", ordinal, len(found), resolved)
	}
	return found[0]
}

func cvArity(n int) *uint16 { v := uint16(n); return &v }

// `f = helper; f()` — a marked function_value callsite resolves through the
// recorded alias at conf 0.85 with callable_alias evidence.
func TestCallableValue_BareAliasResolves(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "f", TypeName: "helper", TypeQualified: "helper", Scope: "main", File: "a.py", Line: 2, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "a.py", StartLine: 1},
		2: {Label: "Function", Name: "helper", File: "a.py", StartLine: 10},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}}
	fileNodeIDs := map[string]map[string][]int64{"a.py": {"main": {1}, "helper": {2}}}
	call := parser.CallRef{CalleeName: "f", CalleeQualified: "f", CallerScope: "main", File: "a.py", Line: 3, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" || edge.Confidence != 0.85 ||
		edge.EvidenceType != "callable_alias" || edge.ReceiverOrigin != "assignment" {
		t.Fatalf("f() -> %+v, want callable_value->2 @0.85 callable_alias/assignment", edge)
	}
}

// `self.cb = helper; self.cb()` where a module-level `def cb` exists — the
// marked callsite MUST reach helper: same_file on the leaf name would claim
// the symbol the field binding shadows.
func TestCallableValue_FieldBindingShadowsDefinedLeaf(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "self.cb", TypeName: "helper", TypeQualified: "helper", Scope: "C.ctor", ObjectScope: "C", File: "a.py", Line: 3, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		2: {Label: "Function", Name: "helper", File: "a.py", StartLine: 1},
		5: {Label: "Class", Name: "C", File: "a.py", StartLine: 4},
		6: {Label: "Method", Name: "run", File: "a.py", ParentID: 5, StartLine: 7},
		7: {Label: "Method", Name: "ctor", File: "a.py", ParentID: 5, StartLine: 5},
		9: {Label: "Function", Name: "cb", File: "a.py", StartLine: 20},
	}
	nodeIDs := map[string][]int64{"helper": {2}, "C": {5}, "run": {6}, "ctor": {7}, "cb": {9}}
	fileNodeIDs := map[string]map[string][]int64{"a.py": {"helper": {2}, "C": {5}, "run": {6}, "ctor": {7}, "cb": {9}}}
	call := parser.CallRef{CalleeName: "cb", CalleeQualified: "self.cb", CallerScope: "C.run", File: "a.py", Line: 9, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{6}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" || edge.Confidence != 0.85 {
		t.Fatalf("self.cb() -> %+v, want callable_value->2 @0.85 (field binding shadows module-level cb)", edge)
	}
}

// `this.f = helper; this.f()` — JS object-field alias resolves to the RHS.
func TestCallableValue_ThisFieldResolves(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "this.f", TypeName: "helper", TypeQualified: "helper", Scope: "C.ctor", ObjectScope: "C", File: "c.js", Line: 3, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		2: {Label: "Function", Name: "helper", File: "c.js", StartLine: 1},
		5: {Label: "Class", Name: "C", File: "c.js", StartLine: 2},
		6: {Label: "Method", Name: "run", File: "c.js", ParentID: 5, StartLine: 5},
		7: {Label: "Method", Name: "ctor", File: "c.js", ParentID: 5, StartLine: 3},
	}
	nodeIDs := map[string][]int64{"helper": {2}, "C": {5}, "run": {6}, "ctor": {7}}
	fileNodeIDs := map[string]map[string][]int64{"c.js": {"helper": {2}, "C": {5}, "run": {6}, "ctor": {7}}}
	call := parser.CallRef{CalleeName: "f", CalleeQualified: "this.f", CallerScope: "C.run", File: "c.js", Line: 6, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{6}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" || edge.EvidenceType != "callable_alias" {
		t.Fatalf("this.f() -> %+v, want callable_value->2 callable_alias", edge)
	}
}

// `f = obj.method; f()` where obj = Obj() is a tracked constructor binding —
// the alias resolves to Obj.method.
func TestCallableValue_QualifiedAliasResolves(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "obj", TypeName: "Obj", TypeQualified: "Obj", Scope: "main", File: "a.py", Line: 2},
		{VarName: "f", TypeName: "method", TypeQualified: "obj.method", Scope: "main", File: "a.py", Line: 3, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "a.py", StartLine: 1},
		3: {Label: "Class", Name: "Obj", File: "a.py", StartLine: 6},
		4: {Label: "Method", Name: "method", File: "a.py", ParentID: 3, StartLine: 8},
	}
	nodeIDs := map[string][]int64{"main": {1}, "Obj": {3}, "method": {4}}
	fileNodeIDs := map[string]map[string][]int64{"a.py": {"main": {1}, "Obj": {3}, "method": {4}}}
	call := parser.CallRef{CalleeName: "f", CalleeQualified: "f", CallerScope: "main", File: "a.py", Line: 4, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 4 || edge.Method != "callable_value" {
		t.Fatalf("f() -> %+v, want callable_value->4 (Obj.method)", edge)
	}
}

// `def wrap(cb): cb()` + `wrap(helper)` — argument→formal flow resolves cb()
// to helper at conf 0.8 with callable_parameter evidence.
func TestCallableValue_ParamFlowResolves(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "cb", IsParameter: true, ParameterIndex: 0, Scope: "wrap", Owner: "wrap", File: "a.py", Line: 1},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "a.py", StartLine: 4},
		2: {Label: "Function", Name: "helper", File: "a.py", StartLine: 10},
		3: {Label: "Function", Name: "wrap", File: "a.py", StartLine: 1},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}, "wrap": {3}}
	fileNodeIDs := map[string]map[string][]int64{"a.py": {"main": {1}, "helper": {2}, "wrap": {3}}}
	calls := []parser.CallRef{
		{CalleeName: "cb", CalleeQualified: "cb", CallerScope: "wrap", File: "a.py", Line: 2, DispatchForm: "function_value"},
		{CalleeName: "wrap", CalleeQualified: "wrap", CallerScope: "main", File: "a.py", Line: 5, ArgumentArity: cvArity(1), ArgumentNames: []string{"helper"}},
	}

	resolved := resolveCalls(t, calls, []int64{3, 1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" || edge.Confidence != 0.8 ||
		edge.EvidenceType != "callable_parameter" || edge.ReceiverOrigin != "assignment" {
		t.Fatalf("cb() -> %+v, want callable_value->2 @0.8 callable_parameter/assignment", edge)
	}
}

// `def wrap(cb): cb()` where a module-level `def cb` exists — the formal
// shadows the symbol, so cb() resolves to the argument (helper), never cb.
func TestCallableValue_ParamShadowsDefinedSymbol(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "cb", IsParameter: true, ParameterIndex: 0, Scope: "wrap", Owner: "wrap", File: "a.py", Line: 1},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "a.py", StartLine: 4},
		2: {Label: "Function", Name: "helper", File: "a.py", StartLine: 10},
		3: {Label: "Function", Name: "wrap", File: "a.py", StartLine: 1},
		9: {Label: "Function", Name: "cb", File: "a.py", StartLine: 20},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}, "wrap": {3}, "cb": {9}}
	fileNodeIDs := map[string]map[string][]int64{"a.py": {"main": {1}, "helper": {2}, "wrap": {3}, "cb": {9}}}
	calls := []parser.CallRef{
		{CalleeName: "cb", CalleeQualified: "cb", CallerScope: "wrap", File: "a.py", Line: 2, DispatchForm: "function_value"},
		{CalleeName: "wrap", CalleeQualified: "wrap", CallerScope: "main", File: "a.py", Line: 5, ArgumentArity: cvArity(1), ArgumentNames: []string{"helper"}},
	}

	resolved := resolveCalls(t, calls, []int64{3, 1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 {
		t.Fatalf("cb() -> target %d, want 2 (helper) — the formal must shadow defined cb(9)", edge.TargetNodeID)
	}
}

// `const w = (cb) => cb()` nested inside `outer` — the formal's Owner is "w"
// while its visibility scope is "outer": the callsite `w(helper)` binds it.
func TestCallableValue_NestedArrowOwnerFlow(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "cb", IsParameter: true, ParameterIndex: 0, Scope: "outer", Owner: "w", File: "x.js", Line: 2},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "outer", File: "x.js", StartLine: 1},
		2: {Label: "Function", Name: "helper", File: "x.js", StartLine: 10},
	}
	nodeIDs := map[string][]int64{"outer": {1}, "helper": {2}}
	fileNodeIDs := map[string]map[string][]int64{"x.js": {"outer": {1}, "helper": {2}}}
	calls := []parser.CallRef{
		{CalleeName: "cb", CalleeQualified: "cb", CallerScope: "outer", File: "x.js", Line: 3, DispatchForm: "function_value"},
		{CalleeName: "w", CalleeQualified: "w", CallerScope: "outer", File: "x.js", Line: 5, ArgumentArity: cvArity(1), ArgumentNames: []string{"helper"}},
	}

	resolved := resolveCalls(t, calls, []int64{1, 1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" {
		t.Fatalf("cb() inside nested arrow -> %+v, want callable_value->2", edge)
	}
}

// `f = helper` AND `f = other` (a conditional rebind) — disagreeing symbol
// writes abstain: no callable_value edge, and since f names no symbol, no
// edge at all.
func TestCallableValue_AmbiguousRebindAbstains(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "f", TypeName: "helper", TypeQualified: "helper", Scope: "main", File: "a.py", Line: 2, ViaSymbol: true},
		{VarName: "f", TypeName: "other", TypeQualified: "other", Scope: "main", File: "a.py", Line: 3, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "a.py", StartLine: 1},
		2: {Label: "Function", Name: "helper", File: "a.py", StartLine: 10},
		3: {Label: "Function", Name: "other", File: "a.py", StartLine: 12},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}, "other": {3}}
	fileNodeIDs := map[string]map[string][]int64{"a.py": {"main": {1}, "helper": {2}, "other": {3}}}
	call := parser.CallRef{CalleeName: "f", CalleeQualified: "f", CallerScope: "main", File: "a.py", Line: 4, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	for _, rc := range resolved {
		if rc.CallsiteOrdinal == 0 {
			t.Fatalf("ambiguous rebind produced an edge: %+v — want abstention", rc)
		}
	}
}

// `f = obj.method` where two classes define `method` — the leaf fallback
// retains both candidates → the marked callsite abstains entirely (the value
// binding forbids a name_match guess).
func TestCallableValue_AmbiguousTargetAbstains(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "f", TypeName: "method", TypeQualified: "obj.method", Scope: "main", File: "a.py", Line: 3, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "a.py", StartLine: 1},
		3: {Label: "Class", Name: "A", File: "a.py", StartLine: 6},
		4: {Label: "Method", Name: "method", File: "a.py", ParentID: 3, StartLine: 8},
		5: {Label: "Class", Name: "B", File: "a.py", StartLine: 10},
		6: {Label: "Method", Name: "method", File: "a.py", ParentID: 5, StartLine: 12},
	}
	nodeIDs := map[string][]int64{"main": {1}, "A": {3}, "B": {5}, "method": {4, 6}}
	fileNodeIDs := map[string]map[string][]int64{"a.py": {"main": {1}, "A": {3}, "B": {5}, "method": {4, 6}}}
	call := parser.CallRef{CalleeName: "f", CalleeQualified: "f", CallerScope: "main", File: "a.py", Line: 4, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	for _, rc := range resolved {
		if rc.CallsiteOrdinal == 0 {
			t.Fatalf("multi-target alias produced an edge: %+v — want abstention", rc)
		}
	}
}

// `wrap(helper)` and `wrap(other)` disagree on the formal's binding — cb()
// abstains rather than picking one.
func TestCallableValue_ParamFlowDisagreementAbstains(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "cb", IsParameter: true, ParameterIndex: 0, Scope: "wrap", Owner: "wrap", File: "a.py", Line: 1},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "a.py", StartLine: 4},
		2: {Label: "Function", Name: "helper", File: "a.py", StartLine: 10},
		3: {Label: "Function", Name: "wrap", File: "a.py", StartLine: 1},
		4: {Label: "Function", Name: "other", File: "a.py", StartLine: 12},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}, "wrap": {3}, "other": {4}}
	fileNodeIDs := map[string]map[string][]int64{"a.py": {"main": {1}, "helper": {2}, "wrap": {3}, "other": {4}}}
	calls := []parser.CallRef{
		{CalleeName: "cb", CalleeQualified: "cb", CallerScope: "wrap", File: "a.py", Line: 2, DispatchForm: "function_value"},
		{CalleeName: "wrap", CalleeQualified: "wrap", CallerScope: "main", File: "a.py", Line: 5, ArgumentArity: cvArity(1), ArgumentNames: []string{"helper"}},
		{CalleeName: "wrap", CalleeQualified: "wrap", CallerScope: "main", File: "a.py", Line: 6, ArgumentArity: cvArity(1), ArgumentNames: []string{"other"}},
	}

	resolved := resolveCalls(t, calls, []int64{3, 1, 1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	for _, rc := range resolved {
		if rc.CallsiteOrdinal == 0 {
			t.Fatalf("disagreeing callsites produced an edge: %+v — want abstention", rc)
		}
	}
}

// `wrap(*args)` — a spread argument makes positional binding unprovable →
// abstain.
func TestCallableValue_ParamFlowSpreadAbstains(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "cb", IsParameter: true, ParameterIndex: 0, Scope: "wrap", Owner: "wrap", File: "a.py", Line: 1},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "a.py", StartLine: 4},
		2: {Label: "Function", Name: "helper", File: "a.py", StartLine: 10},
		3: {Label: "Function", Name: "wrap", File: "a.py", StartLine: 1},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}, "wrap": {3}}
	fileNodeIDs := map[string]map[string][]int64{"a.py": {"main": {1}, "helper": {2}, "wrap": {3}}}
	calls := []parser.CallRef{
		{CalleeName: "cb", CalleeQualified: "cb", CallerScope: "wrap", File: "a.py", Line: 2, DispatchForm: "function_value"},
		{CalleeName: "wrap", CalleeQualified: "wrap", CallerScope: "main", File: "a.py", Line: 5, ArgumentArity: cvArity(1), ArgumentSpread: true},
	}

	resolved := resolveCalls(t, calls, []int64{3, 1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	for _, rc := range resolved {
		if rc.CallsiteOrdinal == 0 {
			t.Fatalf("spread-arg callsite produced an edge: %+v — want abstention", rc)
		}
	}
}

// Full pipeline: parse real source → BuildAssignmentIndex → resolveInternal.
// `def helper(): ... def main(): f = helper; f()` — exercises the parser's
// marking pass, the real CallerScope/AssignmentRef shapes, and the early gate.
func TestCallableValue_EndToEnd_ParsedPython(t *testing.T) {
	resetStrategyIndexes(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "m.py")
	src := "def helper():\n" +
		"    pass\n" +
		"def main():\n" +
		"    f = helper\n" +
		"    f()\n"
	if err := os.WriteFile(path, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	spec := specs.ForExtension(".py")
	if spec == nil {
		t.Fatal("no python spec")
	}
	sf := walker.SourceFile{Path: "m.py", AbsPath: path, Language: spec.Name, Spec: spec}
	res, err := parser.ParseFile(sf, false)
	if err != nil {
		t.Fatalf("ParseFile: %v", err)
	}
	meta := map[int64]NodeMeta{}
	nodeIDs := map[string][]int64{}
	fileNodeIDs := map[string]map[string][]int64{"m.py": {}}
	var helperID int64
	for i, n := range res.Nodes {
		id := int64(i + 1)
		meta[id] = NodeMeta{Label: n.Label, Name: n.Name, File: n.FilePath, ParentID: n.ParentID, StartLine: n.StartLine}
		nodeIDs[n.Name] = append(nodeIDs[n.Name], id)
		fileNodeIDs["m.py"][n.Name] = append(fileNodeIDs["m.py"][n.Name], id)
		if n.Name == "helper" {
			helperID = id
		}
	}
	callerIDs := make([]int64, len(res.Calls))
	for i, c := range res.Calls {
		callerIDs[i] = int64(c.CallerNodeIdx + 1)
	}
	SetAssignmentIndex(BuildAssignmentIndex(res.Assignments))
	resolved, _ := resolveInternal(res.Calls, nodeIDs, fileNodeIDs, callerIDs, res.Imports, nil, true, meta)

	var aliasCallIdx = -1
	for i, c := range res.Calls {
		if c.CalleeName == "f" {
			aliasCallIdx = i
			if c.DispatchForm != "function_value" {
				t.Fatalf("f() DispatchForm = %q, want function_value", c.DispatchForm)
			}
		}
	}
	if aliasCallIdx < 0 {
		t.Fatalf("no f() callsite parsed; calls=%+v", res.Calls)
	}
	edge := edgeFor(t, resolved, aliasCallIdx)
	if edge.TargetNodeID != helperID || edge.Method != "callable_value" ||
		edge.Confidence != 0.85 || edge.EvidenceType != "callable_alias" {
		t.Fatalf("f() -> %+v, want callable_value->%d @0.85 callable_alias", edge, helperID)
	}
}

// `const f = () => helper(); const g = f; g()` at module level — the arrow now
// carries a real node named "f", so the alias chain resolves g() to it.
func TestCallableValue_ArrowAliasChainResolves(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "g", TypeName: "f", TypeQualified: "f", Scope: "", File: "x.js", Line: 3, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		2: {Label: "Function", Name: "helper", File: "x.js", StartLine: 1},
		4: {Label: "Function", Name: "f", File: "x.js", StartLine: 2},
	}
	nodeIDs := map[string][]int64{"helper": {2}, "f": {4}}
	fileNodeIDs := map[string]map[string][]int64{"x.js": {"helper": {2}, "f": {4}}}
	// The alias write is module-level (Scope ""), which ResolveCallableBinding
	// treats as visible from every scope — the callsite sits inside `main`.
	meta[1] = NodeMeta{Label: "Function", Name: "main", File: "x.js", StartLine: 5}
	nodeIDs["main"] = []int64{1}
	fileNodeIDs["x.js"]["main"] = []int64{1}
	call := parser.CallRef{CalleeName: "g", CalleeQualified: "g", CallerScope: "main", File: "x.js", Line: 6, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 4 || edge.Method != "callable_value" {
		t.Fatalf("g() -> %+v, want callable_value->4 (the arrow node f)", edge)
	}
}

// An UNMARKED callsite with a recorded alias still resolves through the 1.96b
// rung (defensive coverage for fixtures/callers that never run the marking
// pass) — the binding is real evidence even without the DispatchForm stamp.
func TestCallableValue_UnmarkedAliasStillResolves(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "f", TypeName: "helper", TypeQualified: "helper", Scope: "main", File: "a.py", Line: 2, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "a.py", StartLine: 1},
		2: {Label: "Function", Name: "helper", File: "a.py", StartLine: 10},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}}
	fileNodeIDs := map[string]map[string][]int64{"a.py": {"main": {1}, "helper": {2}}}
	call := parser.CallRef{CalleeName: "f", CalleeQualified: "f", CallerScope: "main", File: "a.py", Line: 3, DispatchForm: "static"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" {
		t.Fatalf("unmarked f() -> %+v, want callable_value->2 via Strategy 1.96b", edge)
	}
}

// A marked callsite whose binding resolves nothing must not fall to
// name_match: `f = missing; f()` where `f` is ALSO a defined symbol in the
// file. The early gate treats the found binding as authoritative — the call
// abstains rather than claiming the same-named definition.
func TestCallableValue_MarkedCallNeverNameMatches(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "f", TypeName: "missing", TypeQualified: "missing", Scope: "main", File: "a.py", Line: 2, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "a.py", StartLine: 1},
		8: {Label: "Function", Name: "f", File: "b.py", StartLine: 3},
	}
	nodeIDs := map[string][]int64{"main": {1}, "f": {8}}
	fileNodeIDs := map[string]map[string][]int64{"a.py": {"main": {1}}, "b.py": {"f": {8}}}
	call := parser.CallRef{CalleeName: "f", CalleeQualified: "f", CallerScope: "main", File: "a.py", Line: 3, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	for _, rc := range resolved {
		if rc.CallsiteOrdinal == 0 {
			t.Fatalf("marked call with unresolvable alias produced an edge: %+v — want abstention, not name_match->f", rc)
		}
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// HAR-90 item 2 — Java method references / SAM calls, Go function values,
// Kotlin callable references. Same confidence/evidence vocabulary as the
// Python/TS flow: 0.85 callable_alias, 0.8 callable_parameter.
// ─────────────────────────────────────────────────────────────────────────────

// Java: `Runnable r = this::work; r.run()` — the alias RHS is a `this`-bound
// method reference; `r.run()` (extracted as callee "r") resolves to the
// Method `work` on the caller's class.
func TestCallableValue_MethodRefAlias_Java(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "r", TypeName: "work", TypeQualified: "this.work", Scope: "Items.handle", ObjectScope: "Items", File: "Items.java", Line: 5, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Class", Name: "Items", File: "Items.java", StartLine: 1},
		2: {Label: "Method", Name: "handle", File: "Items.java", ParentID: 1, StartLine: 3},
		3: {Label: "Method", Name: "work", File: "Items.java", ParentID: 1, StartLine: 10},
	}
	nodeIDs := map[string][]int64{"Items": {1}, "handle": {2}, "work": {3}}
	fileNodeIDs := map[string]map[string][]int64{"Items.java": {"Items": {1}, "handle": {2}, "work": {3}}}
	call := parser.CallRef{CalleeName: "r", CalleeQualified: "r", CallerScope: "Items.handle", File: "Items.java", Line: 6, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{2}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 3 || edge.Method != "callable_value" || edge.Confidence != 0.85 ||
		edge.EvidenceType != "callable_alias" || edge.ReceiverOrigin != "assignment" {
		t.Fatalf("r.run() -> %+v, want callable_value->3 @0.85 callable_alias/assignment", edge)
	}
}

// Java: `Function<String,Integer> f = Helper::parse` — a class-receiver
// method reference resolves through the class's member, not a bare name.
func TestCallableValue_ClassMethodRef_Java(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "f", TypeName: "parse", TypeQualified: "Helper.parse", Scope: "Items.handle", ObjectScope: "Items", File: "Items.java", Line: 5, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Class", Name: "Items", File: "Items.java", StartLine: 1},
		2: {Label: "Method", Name: "handle", File: "Items.java", ParentID: 1, StartLine: 3},
		4: {Label: "Class", Name: "Helper", File: "Helper.java", StartLine: 1},
		5: {Label: "Method", Name: "parse", File: "Helper.java", ParentID: 4, StartLine: 3},
	}
	nodeIDs := map[string][]int64{"Items": {1}, "handle": {2}, "Helper": {4}, "parse": {5}}
	fileNodeIDs := map[string]map[string][]int64{
		"Items.java":  {"Items": {1}, "handle": {2}},
		"Helper.java": {"Helper": {4}, "parse": {5}},
	}
	call := parser.CallRef{CalleeName: "f", CalleeQualified: "f", CallerScope: "Items.handle", File: "Items.java", Line: 6, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{2}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 5 || edge.Method != "callable_value" || edge.Confidence != 0.85 {
		t.Fatalf("f.apply() -> %+v, want callable_value->5 (Helper.parse)", edge)
	}
}

// Java: `void register(Runnable cb) { cb.run(); }` + `register(this::work)`
// — an unqualified same-class callsite binds the formal; the `this::work`
// method-reference argument resolves against the CALLSITE's class.
func TestCallableValue_ParamMethodRef_Java(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "cb", TypeName: "Runnable", IsParameter: true, ParameterIndex: 0, Scope: "Items.register", ObjectScope: "Items", Owner: "Items.register", File: "Items.java", Line: 4},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Class", Name: "Items", File: "Items.java", StartLine: 1},
		2: {Label: "Method", Name: "work", File: "Items.java", ParentID: 1, StartLine: 3},
		3: {Label: "Method", Name: "register", File: "Items.java", ParentID: 1, StartLine: 4},
		4: {Label: "Method", Name: "handle", File: "Items.java", ParentID: 1, StartLine: 8},
	}
	nodeIDs := map[string][]int64{"Items": {1}, "work": {2}, "register": {3}, "handle": {4}}
	fileNodeIDs := map[string]map[string][]int64{"Items.java": {"Items": {1}, "work": {2}, "register": {3}, "handle": {4}}}
	calls := []parser.CallRef{
		{CalleeName: "cb", CalleeQualified: "cb", CallerScope: "Items.register", File: "Items.java", Line: 5, DispatchForm: "function_value"},
		{CalleeName: "register", CalleeQualified: "register", CallerScope: "Items.handle", File: "Items.java", Line: 9, ArgumentArity: cvArity(1), ArgumentNames: []string{"this::work"}},
	}

	resolved := resolveCalls(t, calls, []int64{3, 4}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" || edge.Confidence != 0.8 ||
		edge.EvidenceType != "callable_parameter" {
		t.Fatalf("cb.run() -> %+v, want callable_value->2 @0.8 callable_parameter", edge)
	}
}

// Java: the same formal bound by a DIFFERENT-class callsite does not leak —
// `Other.run(this::other)` calls a same-named `register` that does not exist
// in Other, so the formal's binding stays unseen → abstain.
func TestCallableValue_ParamForeignCallsiteAbstains_Java(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "cb", TypeName: "Runnable", IsParameter: true, ParameterIndex: 0, Scope: "Items.register", ObjectScope: "Items", Owner: "Items.register", File: "Items.java", Line: 4},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Class", Name: "Items", File: "Items.java", StartLine: 1},
		3: {Label: "Method", Name: "register", File: "Items.java", ParentID: 1, StartLine: 4},
		4: {Label: "Method", Name: "handle", File: "Items.java", ParentID: 1, StartLine: 8},
		5: {Label: "Class", Name: "Other", File: "Items.java", StartLine: 20},
		6: {Label: "Method", Name: "run", File: "Items.java", ParentID: 5, StartLine: 21},
	}
	nodeIDs := map[string][]int64{"Items": {1}, "register": {3}, "handle": {4}, "Other": {5}, "run": {6}}
	fileNodeIDs := map[string]map[string][]int64{"Items.java": {"Items": {1}, "register": {3}, "handle": {4}, "Other": {5}, "run": {6}}}
	calls := []parser.CallRef{
		{CalleeName: "cb", CalleeQualified: "cb", CallerScope: "Items.register", File: "Items.java", Line: 5, DispatchForm: "function_value"},
		// `register(this::work)` inside Other — callerObjectScope "Other" !=
		// "Items" → does not bind Items.register's formal.
		{CalleeName: "register", CalleeQualified: "register", CallerScope: "Other.run", File: "Items.java", Line: 22, ArgumentArity: cvArity(1), ArgumentNames: []string{"this::work"}},
	}

	resolved := resolveCalls(t, calls, []int64{3, 6}, meta, nodeIDs, fileNodeIDs, nil, nil)
	for _, rc := range resolved {
		if rc.CallsiteOrdinal == 0 {
			t.Fatalf("cb() resolved via a foreign-class callsite: %+v — want abstention", rc)
		}
	}
}

// Java: a class-level field alias (`private Runnable r = this::work;`) binds
// both `r.run()` and `this.r.run()` — the bare name is object-scoped so a
// same-named call in a sibling class does NOT see it.
func TestCallableValue_FieldAlias_Java(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "r", TypeName: "work", TypeQualified: "this.work", Scope: "", ObjectScope: "Items", File: "Items.java", Line: 3, ViaSymbol: true},
		{VarName: "this.r", TypeName: "work", TypeQualified: "this.work", Scope: "", ObjectScope: "Items", File: "Items.java", Line: 3, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Class", Name: "Items", File: "Items.java", StartLine: 1},
		2: {Label: "Method", Name: "handle", File: "Items.java", ParentID: 1, StartLine: 4},
		3: {Label: "Method", Name: "work", File: "Items.java", ParentID: 1, StartLine: 10},
		5: {Label: "Class", Name: "Other", File: "Items.java", StartLine: 20},
		6: {Label: "Method", Name: "run", File: "Items.java", ParentID: 5, StartLine: 21},
		7: {Label: "Method", Name: "wire", File: "Items.java", ParentID: 1, StartLine: 12},
	}
	nodeIDs := map[string][]int64{"Items": {1}, "handle": {2}, "work": {3}, "Other": {5}, "run": {6}, "wire": {7}}
	fileNodeIDs := map[string]map[string][]int64{"Items.java": {"Items": {1}, "handle": {2}, "work": {3}, "Other": {5}, "run": {6}, "wire": {7}}}
	calls := []parser.CallRef{
		{CalleeName: "r", CalleeQualified: "r", CallerScope: "Items.handle", File: "Items.java", Line: 5, DispatchForm: "function_value"},
		// Different caller method — caller+target dedup would otherwise
		// collapse it into callsite 0's edge.
		{CalleeName: "this.r", CalleeQualified: "this.r", CallerScope: "Items.wire", File: "Items.java", Line: 13, DispatchForm: "function_value"},
		// Same callee name in a SIBLING class — the Items field alias must
		// not claim it (marked or not, the binding is class-scoped).
		{CalleeName: "r", CalleeQualified: "r", CallerScope: "Other.run", File: "Items.java", Line: 22, DispatchForm: "function_value"},
	}

	resolved := resolveCalls(t, calls, []int64{2, 7, 6}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 3 || edge.Method != "callable_value" {
		t.Fatalf("r.run() -> %+v, want callable_value->3", edge)
	}
	edge = edgeFor(t, resolved, 1)
	if edge.TargetNodeID != 3 || edge.Method != "callable_value" {
		t.Fatalf("this.r.run() -> %+v, want callable_value->3", edge)
	}
	for _, rc := range resolved {
		if rc.CallsiteOrdinal == 2 && rc.Method == "callable_value" {
			t.Fatalf("sibling-class r() claimed the Items field alias: %+v", rc)
		}
	}
}

// Go: `f := helper; f()` — the local alias resolves exactly like the Python
// case.
func TestCallableValue_BareAlias_Go(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "f", TypeName: "helper", TypeQualified: "helper", Scope: "main", File: "m.go", Line: 5, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "m.go", StartLine: 4},
		2: {Label: "Function", Name: "helper", File: "m.go", StartLine: 1},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}}
	fileNodeIDs := map[string]map[string][]int64{"m.go": {"main": {1}, "helper": {2}}}
	call := parser.CallRef{CalleeName: "f", CalleeQualified: "f", CallerScope: "main", File: "m.go", Line: 6, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" || edge.Confidence != 0.85 {
		t.Fatalf("f() -> %+v, want callable_value->2 @0.85", edge)
	}
}

// Go: `var pkgf = helper` (package scope) resolves `pkgf()` inside any
// function — Scope "" bindings are module-visible.
func TestCallableValue_PackageAlias_Go(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "pkgf", TypeName: "helper", TypeQualified: "helper", Scope: "", File: "m.go", Line: 4, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "m.go", StartLine: 6},
		2: {Label: "Function", Name: "helper", File: "m.go", StartLine: 1},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}}
	fileNodeIDs := map[string]map[string][]int64{"m.go": {"main": {1}, "helper": {2}}}
	call := parser.CallRef{CalleeName: "pkgf", CalleeQualified: "pkgf", CallerScope: "main", File: "m.go", Line: 7, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" {
		t.Fatalf("pkgf() -> %+v, want callable_value->2", edge)
	}
}

// Go: `h := Handler{F: helper}; h.F()` — the keyed composite-literal field
// binding (`h.F` → helper) resolves the qualified callsite.
func TestCallableValue_StructField_Go(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "h", TypeName: "Handler", TypeQualified: "Handler", Scope: "main", File: "m.go", Line: 8},
		{VarName: "h.F", TypeName: "helper", TypeQualified: "helper", Scope: "main", File: "m.go", Line: 8, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "m.go", StartLine: 7},
		2: {Label: "Function", Name: "helper", File: "m.go", StartLine: 1},
		3: {Label: "Class", Name: "Handler", File: "m.go", StartLine: 4},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}, "Handler": {3}}
	fileNodeIDs := map[string]map[string][]int64{"m.go": {"main": {1}, "helper": {2}, "Handler": {3}}}
	call := parser.CallRef{CalleeName: "F", CalleeQualified: "h.F", CallerScope: "main", File: "m.go", Line: 9, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" || edge.EvidenceType != "callable_alias" {
		t.Fatalf("h.F() -> %+v, want callable_value->2 callable_alias", edge)
	}
}

// Go: `func wrap(cb func() string) { cb() }` + `wrap(helper)` — the
// func-typed formal binds through the callsite argument at conf 0.8.
func TestCallableValue_ParamFlow_Go(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "cb", IsParameter: true, ParameterIndex: 0, Scope: "wrap", Owner: "wrap", File: "m.go", Line: 5},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "m.go", StartLine: 7},
		2: {Label: "Function", Name: "helper", File: "m.go", StartLine: 1},
		3: {Label: "Function", Name: "wrap", File: "m.go", StartLine: 5},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}, "wrap": {3}}
	fileNodeIDs := map[string]map[string][]int64{"m.go": {"main": {1}, "helper": {2}, "wrap": {3}}}
	calls := []parser.CallRef{
		{CalleeName: "cb", CalleeQualified: "cb", CallerScope: "wrap", File: "m.go", Line: 5, DispatchForm: "function_value"},
		{CalleeName: "wrap", CalleeQualified: "wrap", CallerScope: "main", File: "m.go", Line: 8, ArgumentArity: cvArity(1), ArgumentNames: []string{"helper"}},
	}

	resolved := resolveCalls(t, calls, []int64{3, 1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" || edge.Confidence != 0.8 ||
		edge.EvidenceType != "callable_parameter" {
		t.Fatalf("cb() -> %+v, want callable_value->2 @0.8 callable_parameter", edge)
	}
}

// Go: `wrap(s.serve)` — a method-VALUE argument binds through the receiver's
// tracked type (s → Handler → serve method).
func TestCallableValue_ParamMethodValue_Go(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "cb", IsParameter: true, ParameterIndex: 0, Scope: "wrap", Owner: "wrap", File: "m.go", Line: 5},
		{VarName: "s", TypeName: "Handler", TypeQualified: "Handler", Scope: "main", File: "m.go", Line: 8},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "m.go", StartLine: 7},
		2: {Label: "Function", Name: "helper", File: "m.go", StartLine: 1},
		3: {Label: "Function", Name: "wrap", File: "m.go", StartLine: 5},
		4: {Label: "Class", Name: "Handler", File: "m.go", StartLine: 3},
		5: {Label: "Method", Name: "serve", File: "m.go", ParentID: 4, StartLine: 4},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}, "wrap": {3}, "Handler": {4}, "serve": {5}}
	fileNodeIDs := map[string]map[string][]int64{"m.go": {"main": {1}, "helper": {2}, "wrap": {3}, "Handler": {4}, "serve": {5}}}
	calls := []parser.CallRef{
		{CalleeName: "cb", CalleeQualified: "cb", CallerScope: "wrap", File: "m.go", Line: 5, DispatchForm: "function_value"},
		{CalleeName: "wrap", CalleeQualified: "wrap", CallerScope: "main", File: "m.go", Line: 9, ArgumentArity: cvArity(1), ArgumentNames: []string{"s.serve"}},
	}

	resolved := resolveCalls(t, calls, []int64{3, 1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 5 || edge.Method != "callable_value" || edge.Confidence != 0.8 {
		t.Fatalf("cb() -> %+v, want callable_value->5 (Handler.serve) @0.8", edge)
	}
}

// Kotlin: `val f = ::helper; f()` — the callable_reference alias (qualified
// ".helper") resolves through the bare-leaf fallback.
func TestCallableValue_CallableRef_Kotlin(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "f", TypeName: "helper", TypeQualified: ".helper", Scope: "main", File: "m.kt", Line: 4, ViaSymbol: true},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "m.kt", StartLine: 3},
		2: {Label: "Function", Name: "helper", File: "m.kt", StartLine: 1},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}}
	fileNodeIDs := map[string]map[string][]int64{"m.kt": {"main": {1}, "helper": {2}}}
	call := parser.CallRef{CalleeName: "f", CalleeQualified: "f", CallerScope: "main", File: "m.kt", Line: 5, DispatchForm: "function_value"}

	resolved := resolveCalls(t, []parser.CallRef{call}, []int64{1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" || edge.Confidence != 0.85 {
		t.Fatalf("f() -> %+v, want callable_value->2 @0.85", edge)
	}
}

// Kotlin: `fun wrap(cb: () -> String) { cb() }` + `wrap(::helper)` — the
// callable_reference argument flows through the qualified-arg path.
func TestCallableValue_ParamCallableRef_Kotlin(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "cb", IsParameter: true, ParameterIndex: 0, Scope: "wrap", Owner: "wrap", File: "m.kt", Line: 3},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "m.kt", StartLine: 5},
		2: {Label: "Function", Name: "helper", File: "m.kt", StartLine: 1},
		3: {Label: "Function", Name: "wrap", File: "m.kt", StartLine: 3},
	}
	nodeIDs := map[string][]int64{"main": {1}, "helper": {2}, "wrap": {3}}
	fileNodeIDs := map[string]map[string][]int64{"m.kt": {"main": {1}, "helper": {2}, "wrap": {3}}}
	calls := []parser.CallRef{
		{CalleeName: "cb", CalleeQualified: "cb", CallerScope: "wrap", File: "m.kt", Line: 3, DispatchForm: "function_value"},
		{CalleeName: "wrap", CalleeQualified: "wrap", CallerScope: "main", File: "m.kt", Line: 6, ArgumentArity: cvArity(1), ArgumentNames: []string{"::helper"}},
	}

	resolved := resolveCalls(t, calls, []int64{3, 1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	edge := edgeFor(t, resolved, 0)
	if edge.TargetNodeID != 2 || edge.Method != "callable_value" || edge.Confidence != 0.8 ||
		edge.EvidenceType != "callable_parameter" {
		t.Fatalf("cb() -> %+v, want callable_value->2 @0.8 callable_parameter", edge)
	}
}

// Spread arguments still abstain on every language — `wrap(args...)` leaves
// the formal's binding unverifiable.
func TestCallableValue_ParamSpreadAbstains_Go(t *testing.T) {
	resetStrategyIndexes(t)
	SetAssignmentIndex(BuildAssignmentIndex([]parser.AssignmentRef{
		{VarName: "cb", IsParameter: true, ParameterIndex: 0, Scope: "wrap", Owner: "wrap", File: "m.go", Line: 5},
	}))
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "main", File: "m.go", StartLine: 7},
		3: {Label: "Function", Name: "wrap", File: "m.go", StartLine: 5},
	}
	nodeIDs := map[string][]int64{"main": {1}, "wrap": {3}}
	fileNodeIDs := map[string]map[string][]int64{"m.go": {"main": {1}, "wrap": {3}}}
	calls := []parser.CallRef{
		{CalleeName: "cb", CalleeQualified: "cb", CallerScope: "wrap", File: "m.go", Line: 5, DispatchForm: "function_value"},
		{CalleeName: "wrap", CalleeQualified: "wrap", CallerScope: "main", File: "m.go", Line: 8, ArgumentArity: cvArity(1), ArgumentNames: []string{"args"}, ArgumentSpread: true},
	}

	resolved := resolveCalls(t, calls, []int64{3, 1}, meta, nodeIDs, fileNodeIDs, nil, nil)
	for _, rc := range resolved {
		if rc.CallsiteOrdinal == 0 && rc.Method == "callable_value" {
			t.Fatalf("cb() resolved through a spread callsite: %+v — want abstention", rc)
		}
	}
}
