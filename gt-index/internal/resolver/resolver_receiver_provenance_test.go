package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ----------------------------------------------------------------------------
// CALL-SITE RECEIVER PROVENANCE (W-B, 2026-07-09).
//
// The receiver-aware resolution rungs (1.75 self/this/Self CHA, 1.93 import_type,
// 2b field_type, 1.94a param_type, 1.96 assignment type_flow, 1.97 return_type)
// already resolve obj.method() to the RIGHT class by PROVING the receiver's type.
// Historically the resolved receiver type was computed then DISCARDED — the edge
// recorded only method/confidence/tier/evidence, never WHICH receiver type produced
// the resolution. These tests pin the additive call-site provenance: a
// receiver-PROVEN fact edge now carries ResolvedCall.ReceiverType = the class name
// the resolver proved the receiver to be. It is serialized onto edges.metadata as a
// `receiver_type=<T>` tag (main.go) — the SAME `;`-separated key=value convention the
// promote pass uses for `dataflow=`.
//
// LEAK-SAFE INVARIANT (correct-or-quiet): ReceiverType is non-empty IFF the edge is a
// receiver-PROVEN fact. An ABSTAIN (ambiguous same-named receiver) or an UNPROVEN
// cross-scope fallback (RS6) mints NO provenance — a guess never carries a
// fact-shaped receiver tag. These tests exercise Python, Go, and TypeScript through
// the language-uniform graph facts (the resolver is language-agnostic; the language
// only changes the qualifier form — self./this./named-receiver — the parser feeds it).
// Nothing here keys on a repo/task/gold/FAIL_TO_PASS.
// ----------------------------------------------------------------------------

// (1) Python assignment type_flow (Strategy 1.96): a = Beta(); a.run() resolves to
// Beta.run — and now records ReceiverType="Beta". Reuses resolveRunReceiver (the
// Alpha.run/Beta.run confusion fixture) from resolver_receiver_type_test.go.
func TestReceiverProvenance_Py_AssignmentTypeFlow(t *testing.T) {
	got := resolveRunReceiver(t,
		[]parser.AssignmentRef{{VarName: "a", TypeName: "Beta", Scope: "use", File: "m.py"}},
		"a.run")
	if got == nil {
		t.Fatalf("a=Beta(); a.run() did not resolve to any run method")
	}
	if got.TargetNodeID != 2 {
		t.Fatalf("resolution regressed: a.run() -> id %d; want Beta.run id 2", got.TargetNodeID)
	}
	if got.ReceiverType != "Beta" {
		t.Errorf("assignment type_flow provenance: ReceiverType=%q, want %q (resolved via receiver type Beta)",
			got.ReceiverType, "Beta")
	}
}

// (2) Python self.method() CHA (Strategy 1.75): a method calling self.helper() resolves
// to the enclosing class's helper — provenance = the enclosing class name.
func TestReceiverProvenance_Py_SelfCHA(t *testing.T) {
	fm := BuildFileMap([]string{"svc.py"}, []string{"python"})
	nodeIDs := map[string][]int64{"handler": {1}, "helper": {2}, "Service": {10}}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.py": {"handler": {1}, "helper": {2}, "Service": {10}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", Name: "handler", File: "svc.py", ParentID: 10, StartLine: 2},
		2:  {Label: "Method", Name: "helper", File: "svc.py", ParentID: 10, StartLine: 5},
		10: {Label: "Class", Name: "Service", File: "svc.py", StartLine: 1},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "helper", CalleeQualified: "self.helper", Line: 3, File: "svc.py"},
	}
	resolved := Resolve(calls, nodeIDs, fileNodeIDs, []int64{1}, nil, fm, meta)
	got, ok := hasEdge(resolved, 1, 2)
	if !ok {
		t.Fatalf("self.helper() did not resolve to Service.helper; resolved=%+v", resolved)
	}
	if got.ReceiverType != "Service" {
		t.Errorf("self CHA provenance: ReceiverType=%q, want %q (enclosing class)", got.ReceiverType, "Service")
	}
}

// (3) Python param_type (Strategy 1.94a): def handler(c: Client): c.fetch() resolves to
// Client.fetch via the declared param type — provenance = "Client".
func TestReceiverProvenance_Py_ParamType(t *testing.T) {
	fm := BuildFileMap([]string{"caller.py", "client.py"}, []string{"python", "python"})
	nodeIDs := map[string][]int64{"handler": {1}, "Client": {30}, "fetch": {3}}
	fileNodeIDs := map[string]map[string][]int64{
		"caller.py": {"handler": {1}},
		"client.py": {"Client": {30}, "fetch": {3}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "handler", File: "caller.py"},
		30: {Label: "Class", Name: "Client", File: "client.py", StartLine: 1},
		3:  {Label: "Method", Name: "fetch", File: "client.py", ParentID: 30, StartLine: 2},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "fetch", CalleeQualified: "c.fetch", Line: 5, File: "caller.py"},
	}
	SetParamTypeIndex(map[int64]map[string]string{1: {"c": "Client"}})
	defer SetParamTypeIndex(nil)
	resolved := Resolve(calls, nodeIDs, fileNodeIDs, []int64{1}, nil, fm, meta)
	got, ok := hasEdge(resolved, 1, 3)
	if !ok {
		t.Fatalf("c.fetch() did not resolve to Client.fetch; resolved=%+v", resolved)
	}
	if got.EvidenceType != "param_type" {
		t.Fatalf("resolution regressed: evidence=%q want param_type", got.EvidenceType)
	}
	if got.ReceiverType != "Client" {
		t.Errorf("param_type provenance: ReceiverType=%q, want %q", got.ReceiverType, "Client")
	}
}

// (4) Python field_type (Strategy 2b): self.client: HttpClient; self.client.get() resolves
// to HttpClient.get via the declared field type — provenance = "HttpClient".
func TestReceiverProvenance_Py_FieldType(t *testing.T) {
	fm := BuildFileMap([]string{"svc.py", "net.py"}, []string{"python", "python"})
	nodeIDs := map[string][]int64{"handler": {1}, "Service": {10}, "HttpClient": {30}, "get": {3}}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.py": {"handler": {1}, "Service": {10}},
		"net.py": {"HttpClient": {30}, "get": {3}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", Name: "handler", File: "svc.py", ParentID: 10, StartLine: 5},
		10: {Label: "Class", Name: "Service", File: "svc.py", StartLine: 1},
		30: {Label: "Class", Name: "HttpClient", File: "net.py", StartLine: 1},
		3:  {Label: "Method", Name: "get", File: "net.py", ParentID: 30, StartLine: 2},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "self.client.get", Line: 6, File: "svc.py"},
	}
	SetFieldTypeIndex(map[int64]map[string]string{10: {"client": "HttpClient"}})
	defer SetFieldTypeIndex(nil)
	resolved := Resolve(calls, nodeIDs, fileNodeIDs, []int64{1}, nil, fm, meta)
	got, ok := hasEdge(resolved, 1, 3)
	if !ok {
		t.Fatalf("self.client.get() did not resolve to HttpClient.get; resolved=%+v", resolved)
	}
	if got.EvidenceType != "field_type" {
		t.Fatalf("resolution regressed: evidence=%q want field_type", got.EvidenceType)
	}
	if got.ReceiverType != "HttpClient" {
		t.Errorf("field_type provenance: ReceiverType=%q, want %q", got.ReceiverType, "HttpClient")
	}
}

// (5) Go named-receiver CHA (Strategy 1.75 via NodeMeta.ReceiverName): func (r *T) M() {
// r.helper() } resolves to T.helper — the Go analogue of self/this. Provenance = "T".
func TestReceiverProvenance_Go_NamedReceiverCHA(t *testing.T) {
	fm := BuildFileMap([]string{"t.go"}, []string{"go"})
	nodeIDs := map[string][]int64{"M": {1}, "helper": {2}, "T": {10}}
	fileNodeIDs := map[string]map[string][]int64{
		"t.go": {"M": {1}, "helper": {2}, "T": {10}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", Name: "M", File: "t.go", ParentID: 10, ReceiverName: "r", StartLine: 2},
		2:  {Label: "Method", Name: "helper", File: "t.go", ParentID: 10, StartLine: 5},
		10: {Label: "Struct", Name: "T", File: "t.go", StartLine: 1},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "helper", CalleeQualified: "r.helper", Line: 3, File: "t.go"},
	}
	resolved := Resolve(calls, nodeIDs, fileNodeIDs, []int64{1}, nil, fm, meta)
	got, ok := hasEdge(resolved, 1, 2)
	if !ok {
		t.Fatalf("r.helper() did not resolve to T.helper; resolved=%+v", resolved)
	}
	if got.ReceiverType != "T" {
		t.Errorf("Go named-receiver CHA provenance: ReceiverType=%q, want %q", got.ReceiverType, "T")
	}
}

// (6) TypeScript this.field.method() field_type (Strategy 2b via the this. prefix):
// this.client.get() with field client: HttpClient resolves to HttpClient.get.
func TestReceiverProvenance_TS_ThisFieldType(t *testing.T) {
	fm := BuildFileMap([]string{"svc.ts", "net.ts"}, []string{"typescript", "typescript"})
	nodeIDs := map[string][]int64{"handler": {1}, "Service": {10}, "HttpClient": {30}, "get": {3}}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.ts": {"handler": {1}, "Service": {10}},
		"net.ts": {"HttpClient": {30}, "get": {3}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", Name: "handler", File: "svc.ts", ParentID: 10, StartLine: 5},
		10: {Label: "Class", Name: "Service", File: "svc.ts", StartLine: 1},
		30: {Label: "Class", Name: "HttpClient", File: "net.ts", StartLine: 1},
		3:  {Label: "Method", Name: "get", File: "net.ts", ParentID: 30, StartLine: 2},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "this.client.get", Line: 6, File: "svc.ts"},
	}
	SetFieldTypeIndex(map[int64]map[string]string{10: {"client": "HttpClient"}})
	defer SetFieldTypeIndex(nil)
	resolved := Resolve(calls, nodeIDs, fileNodeIDs, []int64{1}, nil, fm, meta)
	got, ok := hasEdge(resolved, 1, 3)
	if !ok {
		t.Fatalf("this.client.get() did not resolve to HttpClient.get; resolved=%+v", resolved)
	}
	if got.EvidenceType != "field_type" {
		t.Fatalf("resolution regressed: evidence=%q want field_type", got.EvidenceType)
	}
	if got.ReceiverType != "HttpClient" {
		t.Errorf("TS field_type provenance: ReceiverType=%q, want %q", got.ReceiverType, "HttpClient")
	}
}

// (7) ABSTAIN → NO PROVENANCE (leak-safety): a receiver type name that matches N>1
// internal classes with no import winner is ambiguous → the resolver abstains and no
// receiver-proven edge is minted. Whatever DOES resolve (a receiver-BLIND rung) must
// carry ReceiverType="" — a guess never carries a fact-shaped receiver tag.
func TestReceiverProvenance_AbstainNoProvenance(t *testing.T) {
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
	SetParamTypeIndex(map[int64]map[string]string{1: {"c": "Client"}}) // param c: Client (ambiguous type name)
	defer SetParamTypeIndex(nil)
	// NO import of Client → the type name "Client" cannot be disambiguated → ABSTAIN.
	resolved := Resolve(calls, nodeIDs, fileNodeIDs, []int64{1}, nil, fm, meta)
	for _, r := range resolved {
		if r.ReceiverType != "" {
			t.Errorf("abstain leaked provenance: an ambiguous receiver minted ReceiverType=%q on edge %+v", r.ReceiverType, r)
		}
	}
}

// (8) UNPROVEN CROSS-SCOPE → NO PROVENANCE (RS6, correct-or-quiet): `a = Beta()` written
// in function `other`; `a.run()` called from `use`. The type is borrowed from a DIFFERENT
// function's write — NOT proven at this call site — so the edge is demoted to CANDIDATE
// and mints NO receiver provenance (present provenance ⟺ receiver-PROVEN fact).
func TestReceiverProvenance_CrossScopeNoProvenance(t *testing.T) {
	got := resolveRunReceiver(t,
		[]parser.AssignmentRef{{VarName: "a", TypeName: "Beta", Scope: "other", File: "m.py"}},
		"a.run")
	if got == nil {
		t.Fatalf("a.run() did not resolve via the cross-scope fallback")
	}
	if got.ReceiverType != "" {
		t.Errorf("cross-scope (unproven) leaked provenance: ReceiverType=%q, want \"\" (type borrowed from a different function is not proven at this call site)",
			got.ReceiverType)
	}
}
