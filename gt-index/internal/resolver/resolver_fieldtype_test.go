package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ----------------------------------------------------------------------------
// Rung 2b: declared-FIELD-type receiver resolution (XTA over the field-type set).
//
// A typed `self.<field>.method()` call where the field has a DECLARED type
// annotation (`client: HttpClient`) but is NOT locally assigned (`self.client =
// HttpClient()` statement absent — injected/inherited/annotation-only) currently
// falls ALL the way to the rung-6 demote tail and is emitted as name_match conf
// 0.2-0.6 (a NAME GUESS). Rung 2b reads the declared field type the parser
// already wrote (class_field property `name: Type`, today discarded at the colon
// by promote.go:367-369), resolves the field's class, and finds the method via
// the SAME CHA primitive (lookupMethodWithInheritance) the other rungs use.
//
// XTA: Tip & Palsberg, OOPSLA 2000 (+88% precision over RTA) — propagate the
// declared-type set; the matcher does not re-derive types, it propagates the
// fact the parser already wrote. Correct-or-quiet: emit ONLY when the field type
// resolves to a real internal class AND CHA finds the method; else fall through.
//
// RED before the rung exists (the SetFieldTypeIndex / BuildFieldTypeIndex
// symbols are absent → compile failure proving the rung is absent), GREEN after.
// ----------------------------------------------------------------------------

// TestResolve_FieldType_DeclaredFieldDisambiguatesAmbiguousMethod is THE core
// red→green ambiguity discriminator. Two classes HttpClient (id 3, net.py) and
// CacheClient (id 5, cache.py) BOTH define get() (ids 4 and 6). The caller
// handler (id 1, ParentID=10 class Service) makes self.client.get(). Service
// declares field `client: HttpClient`. WITHOUT 2b the call is name_match to an
// arbitrary same-named get (ambiguous; could pick CacheClient.get id 6). WITH 2b
// the declared field type proves the receiver is HttpClient → HttpClient.get id 4.
func TestResolve_FieldType_DeclaredFieldDisambiguatesAmbiguousMethod(t *testing.T) {
	files := []string{"svc.py", "net.py", "cache.py"}
	langs := []string{"python", "python", "python"}
	fm := BuildFileMap(files, langs)

	// `get` is non-unique (defined in TWO classes) — impl_method/unique_method
	// skip or cap, and the tail would emit a name_match to an arbitrary get.
	nodeIDs := map[string][]int64{
		"handler":     {1},
		"Service":     {10},
		"HttpClient":  {3},
		"CacheClient": {5},
		"get":         {4, 6},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.py":   {"handler": {1}, "Service": {10}},
		"net.py":   {"HttpClient": {3}, "get": {4}},
		"cache.py": {"CacheClient": {5}, "get": {6}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", File: "svc.py", Name: "handler", ParentID: 10},
		10: {Label: "Class", File: "svc.py", Name: "Service"},
		3:  {Label: "Class", File: "net.py", Name: "HttpClient"},
		4:  {Label: "Method", File: "net.py", Name: "get", ParentID: 3},
		5:  {Label: "Class", File: "cache.py", Name: "CacheClient"},
		6:  {Label: "Method", File: "cache.py", Name: "get", ParentID: 5},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "self.client.get", Line: 5, File: "svc.py"},
	}
	callerIDs := []int64{1}

	// Service (class node 10) declares field `client: HttpClient`. The index is
	// keyed by the OWNING CLASS node id (10), not the caller (1).
	SetFieldTypeIndex(map[int64]map[string]string{10: {"client": "HttpClient"}})
	defer SetFieldTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	if len(resolved) != 1 {
		t.Fatalf("expected 1 resolved edge, got %d: %+v", len(resolved), resolved)
	}
	r := resolved[0]
	if r.Method != "type_flow" {
		t.Errorf("method = %q (conf %.2f), want type_flow — rung 2b should mint a fact from the declared field type", r.Method, r.Confidence)
	}
	if r.EvidenceType != "field_type" {
		t.Errorf("evidence = %q, want field_type (auditable/separable from param_type)", r.EvidenceType)
	}
	if r.Confidence != 0.9 {
		t.Errorf("confidence = %.2f, want 0.9 (declared field type = source fact)", r.Confidence)
	}
	if r.TrustTier != "CERTIFIED" {
		t.Errorf("tier = %q, want CERTIFIED (tierFor(0.9))", r.TrustTier)
	}
	if r.TargetNodeID != 4 {
		t.Errorf("target = %d, want 4 (HttpClient.get) — NOT 6 (CacheClient.get); the field type proves the receiver", r.TargetNodeID)
	}
	if r.ReceiverType != "HttpClient" || r.ReceiverOrigin != "field_annotation" {
		t.Errorf("receiver evidence = %q/%q, want HttpClient/field_annotation", r.ReceiverType, r.ReceiverOrigin)
	}
}

// (b) field type UNKNOWN / not in index → NO field_type edge (falls through to
// the tail demote). Correct-or-quiet: do not guess when the receiver is unknown.
func TestResolve_FieldType_UnknownFieldTypeFallsThrough(t *testing.T) {
	files := []string{"svc.py", "net.py", "cache.py"}
	langs := []string{"python", "python", "python"}
	fm := BuildFileMap(files, langs)

	nodeIDs := map[string][]int64{
		"handler": {1}, "Service": {10},
		"HttpClient": {3}, "CacheClient": {5}, "get": {4, 6},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.py":   {"handler": {1}, "Service": {10}},
		"net.py":   {"HttpClient": {3}, "get": {4}},
		"cache.py": {"CacheClient": {5}, "get": {6}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", File: "svc.py", Name: "handler", ParentID: 10},
		10: {Label: "Class", File: "svc.py", Name: "Service"},
		3:  {Label: "Class", File: "net.py", Name: "HttpClient"},
		4:  {Label: "Method", File: "net.py", Name: "get", ParentID: 3},
		5:  {Label: "Class", File: "cache.py", Name: "CacheClient"},
		6:  {Label: "Method", File: "cache.py", Name: "get", ParentID: 5},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "self.client.get", Line: 5, File: "svc.py"},
	}
	callerIDs := []int64{1}

	// No field-type entry for `client` → rung 2b must not fire.
	SetFieldTypeIndex(map[int64]map[string]string{10: {"other": "SomeType"}})
	defer SetFieldTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	for _, r := range resolved {
		if r.EvidenceType == "field_type" {
			t.Errorf("rung 2b minted a field_type edge with NO declared field type: %+v", r)
		}
	}
}

// (c) builtin self.<field> whose declared type is dict and method is get → rung
// 2b must NOT mint (dict is not an internal class node → fall through →
// builtinQualified DROP at the tail). Preserves the stdlib-shadow bar (§2.5).
func TestResolve_FieldType_BuiltinTypedFieldNotMinted(t *testing.T) {
	files := []string{"svc.py"}
	langs := []string{"python"}
	fm := BuildFileMap(files, langs)

	// `dict` has NO internal class node. `get` is a builtin method name.
	nodeIDs := map[string][]int64{
		"handler": {1}, "Service": {10},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.py": {"handler": {1}, "Service": {10}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", File: "svc.py", Name: "handler", ParentID: 10},
		10: {Label: "Class", File: "svc.py", Name: "Service"},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "self.cache.get", Line: 5, File: "svc.py"},
	}
	callerIDs := []int64{1}

	SetFieldTypeIndex(map[int64]map[string]string{10: {"cache": "dict"}})
	defer SetFieldTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	for _, r := range resolved {
		if r.EvidenceType == "field_type" {
			t.Errorf("rung 2b minted a field_type edge for a builtin-typed field (dict): %+v", r)
		}
		if r.TargetNodeID != 0 {
			t.Errorf("expected the builtin call to be DROPPED, got edge to %d: %+v", r.TargetNodeID, r)
		}
	}
}

// (d) AMBIGUOUS field type: field declared `client: Client` where Client names 2
// internal classes with no import/same-file winner → ABSTAIN, no edge.
func TestResolve_FieldType_AmbiguousFieldTypeAbstains(t *testing.T) {
	files := []string{"svc.py", "a.py", "b.py"}
	langs := []string{"python", "python", "python"}
	fm := BuildFileMap(files, langs)

	// TWO internal classes named `Client`, each with run(). No same-file/import
	// disambiguator for the field type → 2b must abstain (emit no edge).
	nodeIDs := map[string][]int64{
		"handler": {1}, "Service": {10},
		"Client": {3, 5}, "run": {4, 6},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.py": {"handler": {1}, "Service": {10}},
		"a.py":   {"Client": {3}, "run": {4}},
		"b.py":   {"Client": {5}, "run": {6}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", File: "svc.py", Name: "handler", ParentID: 10},
		10: {Label: "Class", File: "svc.py", Name: "Service"},
		3:  {Label: "Class", File: "a.py", Name: "Client"},
		4:  {Label: "Method", File: "a.py", Name: "run", ParentID: 3},
		5:  {Label: "Class", File: "b.py", Name: "Client"},
		6:  {Label: "Method", File: "b.py", Name: "run", ParentID: 5},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "run", CalleeQualified: "self.client.run", Line: 5, File: "svc.py"},
	}
	callerIDs := []int64{1}

	SetFieldTypeIndex(map[int64]map[string]string{10: {"client": "Client"}})
	defer SetFieldTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	for _, r := range resolved {
		if r.EvidenceType == "field_type" {
			t.Errorf("rung 2b minted a field_type edge on an AMBIGUOUS field type (2 same-named classes): %+v", r)
		}
	}
}

// CHA-over-inheritance on a typed field: the field's declared class does NOT
// define the method, but its SUPERCLASS does → 2b resolves via the inheritance
// walk (the shared lookupMethodWithInheritance primitive), conf 0.95 inherited?
// No — 2b uses type_flow/0.9 uniformly; the point here is that an inherited
// method on a typed field still resolves (CHA reused).
func TestResolve_FieldType_InheritedMethodOnTypedField(t *testing.T) {
	files := []string{"svc.py", "net.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	// HttpClient (id 3) extends BaseClient (id 7); send() is defined on BaseClient.
	nodeIDs := map[string][]int64{
		"handler": {1}, "Service": {10},
		"HttpClient": {3}, "BaseClient": {7}, "send": {8},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.py": {"handler": {1}, "Service": {10}},
		"net.py": {"HttpClient": {3}, "BaseClient": {7}, "send": {8}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", File: "svc.py", Name: "handler", ParentID: 10},
		10: {Label: "Class", File: "svc.py", Name: "Service"},
		3:  {Label: "Class", File: "net.py", Name: "HttpClient"},
		7:  {Label: "Class", File: "net.py", Name: "BaseClient"},
		8:  {Label: "Method", File: "net.py", Name: "send", ParentID: 7},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "send", CalleeQualified: "self.client.send", Line: 5, File: "svc.py"},
	}
	callerIDs := []int64{1}

	// HttpClient (3) → BaseClient (7) in the inheritance map.
	SetInheritanceMap(map[int64][]int64{3: {7}})
	defer SetInheritanceMap(nil)
	SetFieldTypeIndex(map[int64]map[string]string{10: {"client": "HttpClient"}})
	defer SetFieldTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	if len(resolved) != 1 {
		t.Fatalf("expected 1 resolved edge, got %d: %+v", len(resolved), resolved)
	}
	r := resolved[0]
	if r.EvidenceType != "field_type" || r.TargetNodeID != 8 {
		t.Errorf("got %+v, want field_type edge to 8 (BaseClient.send via CHA inheritance walk)", r)
	}
}

// BuildFieldTypeIndex unit test: extract a type ONLY from the `name: Type`
// annotation shape; SKIP the `name = expr` assignment shape (no declared type —
// that is rung 1.96's scope-aware job, not 2b's). The value carries the OWNING
// CLASS node, so the index is keyed by class node id.
func TestBuildFieldTypeIndex_AnnotationOnlySkipsAssignment(t *testing.T) {
	// node 10 = a Class; class_field props attach to the class node.
	nodeDBIDs := []int64{0, 100} // NodeIdx 0 -> dbid 0 (skip), NodeIdx 1 -> dbid 100
	props := []parser.PropertyRef{
		{NodeIdx: 1, Kind: "class_field", Value: "client: HttpClient"}, // annotation → keep
		{NodeIdx: 1, Kind: "class_field", Value: "x = CharField(100)"}, // assignment → SKIP
		{NodeIdx: 1, Kind: "param", Value: "p: NotAField"},             // wrong kind → SKIP
	}
	idx := BuildFieldTypeIndex(props, nodeDBIDs)
	got, ok := idx[100]
	if !ok {
		t.Fatalf("expected class node 100 in field index, got %+v", idx)
	}
	if got["client"] != "HttpClient" {
		t.Errorf("client type = %q, want HttpClient", got["client"])
	}
	if _, present := got["x"]; present {
		t.Errorf("assignment-shape field `x` must be SKIPPED (no declared type), got %q", got["x"])
	}
	if _, present := got["p"]; present {
		t.Errorf("a `param` property must not enter the FIELD index")
	}
}

// RESOLVER-PATH test (NOT end-to-end generality): given a POPULATED field-type index
// + a self/this qualifier, the resolver resolves field.method() language-agnostically,
// and stripTypeWrapper handles the *T/&T/plain type-string variations. It INJECTS the
// index directly and uses self/this for every case, so it does NOT exercise
// BuildFieldTypeIndex (which parses Python/Rust colon-annotation fields ONLY) or the
// Go-receiver shape gate — those are the Python+Rust scope boundary (see
// TestBuildFieldTypeIndex_LanguageScope below + the rung-2b SCOPE comment in resolver.go).
// The case labels denote the type-STRING shape exercised, not a claim that the rung
// fires end-to-end on that language.
func TestResolve_FieldType_ResolverPath(t *testing.T) {
	cases := []struct {
		name      string
		file      string
		qualifier string // self / this
		fieldType string // raw declared type as the parser would record it
	}{
		{"go_pointer_field", "svc.go", "self", "*HttpClient"},
		{"rust_field", "svc.rs", "self", "HttpClient"},
		{"ts_class_field", "svc.ts", "this", "HttpClient"},
		{"py_annotated", "svc.py", "self", "HttpClient"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			fm := BuildFileMap([]string{c.file, "net.py"}, []string{"go", "python"})
			nodeIDs := map[string][]int64{
				"handler": {1}, "Service": {10},
				"HttpClient": {3}, "CacheClient": {5}, "get": {4, 6},
			}
			fileNodeIDs := map[string]map[string][]int64{
				c.file:     {"handler": {1}, "Service": {10}},
				"net.py":   {"HttpClient": {3}, "get": {4}},
				"cache.py": {"CacheClient": {5}, "get": {6}},
			}
			meta := map[int64]NodeMeta{
				1:  {Label: "Method", File: c.file, Name: "handler", ParentID: 10},
				10: {Label: "Class", File: c.file, Name: "Service"},
				3:  {Label: "Class", File: "net.py", Name: "HttpClient"},
				4:  {Label: "Method", File: "net.py", Name: "get", ParentID: 3},
				5:  {Label: "Class", File: "cache.py", Name: "CacheClient"},
				6:  {Label: "Method", File: "cache.py", Name: "get", ParentID: 5},
			}
			calls := []parser.CallRef{
				{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: c.qualifier + ".client.get", Line: 5, File: c.file},
			}
			callerIDs := []int64{1}
			SetFieldTypeIndex(map[int64]map[string]string{10: {"client": c.fieldType}})
			defer SetFieldTypeIndex(nil)

			resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
			if len(resolved) != 1 {
				t.Fatalf("[%s] expected 1 resolved edge, got %d: %+v", c.name, len(resolved), resolved)
			}
			r := resolved[0]
			if r.EvidenceType != "field_type" || r.TargetNodeID != 4 {
				t.Errorf("[%s] got %+v, want field_type edge to 4 (HttpClient.get)", c.name, r)
			}
		})
	}
}

// TestBuildFieldTypeIndex_LanguageScope pins the HONEST end-to-end scope of rung 2b after
// the Go+TS extension: BuildFieldTypeIndex now DRIVES four field shapes (Python/Rust colon,
// Go space-separated struct field, TS access-modifier field) and ABSTAINS on the
// non-field-declaration shapes (Go embedded field, assignment). It drives the REAL
// BuildFieldTypeIndex on each language's class_field value.
func TestBuildFieldTypeIndex_LanguageScope(t *testing.T) {
	check := func(val string) map[string]string {
		return BuildFieldTypeIndex(
			[]parser.PropertyRef{{Kind: "class_field", Value: val, NodeIdx: 0}},
			[]int64{10},
		)[10]
	}
	// Python/Rust colon-annotation field: KEPT under the field name.
	if got := check("client: HttpClient"); got["client"] != "HttpClient" {
		t.Errorf("py/rust colon field: got %+v, want client->HttpClient", got)
	}
	// Go space-separated struct field (`Field *Type`, no colon): now POPULATES, name=field,
	// type=Type via the pointer-stripping stripTypeWrapper.
	if got := check("Client *HttpClient"); got["Client"] != "HttpClient" {
		t.Errorf("go pointer struct field: got %+v, want Client->HttpClient", got)
	}
	if got := check("Client HttpClient"); got["Client"] != "HttpClient" {
		t.Errorf("go value struct field: got %+v, want Client->HttpClient", got)
	}
	if got := check("Clients []*HttpClient"); got["Clients"] != "HttpClient" {
		t.Errorf("go slice struct field: got %+v, want Clients->HttpClient", got)
	}
	// Go embedded field (single token, type only — NO field name): ABSTAINS.
	if got := check("HttpClient"); len(got) != 0 {
		t.Errorf("go embedded field (no name) must NOT index: got %+v", got)
	}
	// Go multi-name group (`A, B Type` — ambiguous which name owns the type): ABSTAINS.
	if got := check("A, B HttpClient"); len(got) != 0 {
		t.Errorf("go multi-name group must NOT index (ambiguous): got %+v", got)
	}
	// TS access-modifier field: now indexed under the REAL field name (modifier stripped).
	if got := check("private client: HttpClient"); got["client"] != "HttpClient" {
		t.Errorf("ts private field: got %+v, want client->HttpClient (modifier stripped)", got)
	}
	if got := check("public readonly client: HttpClient"); got["client"] != "HttpClient" {
		t.Errorf("ts public-readonly field: got %+v, want client->HttpClient (modifiers stripped)", got)
	}
}

// TestResolve_FieldType_GoReceiverVar is the Go-receiver red→green: a Go method
// `func (r *Service) handle()` calls `r.client.Get()`. `Get` is defined on BOTH HttpClient
// (id 4) and CacheClient (id 6) — without rung 2b the tail would name_match to an arbitrary
// Get. Service (struct node 10) declares field `client` of type `HttpClient`, and the
// caller's receiver var is `r` (carried in NodeMeta.ReceiverName, as BuildNodeMeta derives
// it from the Go signature). Rung 2b accepts the `r.` receiver prefix (the Go analogue of
// self/this), resolves `client`→HttpClient, and finds HttpClient.Get (id 4), NOT id 6.
func TestResolve_FieldType_GoReceiverVar(t *testing.T) {
	files := []string{"svc.go", "net.go", "cache.go"}
	langs := []string{"go", "go", "go"}
	fm := BuildFileMap(files, langs)

	nodeIDs := map[string][]int64{
		"handle": {1}, "Service": {10},
		"HttpClient": {3}, "CacheClient": {5}, "Get": {4, 6},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.go":   {"handle": {1}, "Service": {10}},
		"net.go":   {"HttpClient": {3}, "Get": {4}},
		"cache.go": {"CacheClient": {5}, "Get": {6}},
	}
	// ReceiverName "r" on the caller method (id 1) — the Go analogue of self/this.
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", File: "svc.go", Name: "handle", ParentID: 10, ReceiverName: "r"},
		10: {Label: "Struct", File: "svc.go", Name: "Service"},
		3:  {Label: "Struct", File: "net.go", Name: "HttpClient"},
		4:  {Label: "Method", File: "net.go", Name: "Get", ParentID: 3},
		5:  {Label: "Struct", File: "cache.go", Name: "CacheClient"},
		6:  {Label: "Method", File: "cache.go", Name: "Get", ParentID: 5},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "Get", CalleeQualified: "r.client.Get", Line: 5, File: "svc.go"},
	}
	callerIDs := []int64{1}

	// Service (struct 10) declares Go field `client` → HttpClient (as BuildFieldTypeIndex
	// now parses `Client *HttpClient` / `client HttpClient`).
	SetFieldTypeIndex(map[int64]map[string]string{10: {"client": "HttpClient"}})
	defer SetFieldTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	if len(resolved) != 1 {
		t.Fatalf("expected 1 resolved edge, got %d: %+v", len(resolved), resolved)
	}
	r := resolved[0]
	if r.Method != "type_flow" || r.EvidenceType != "field_type" {
		t.Errorf("got method=%q evidence=%q, want type_flow/field_type", r.Method, r.EvidenceType)
	}
	if r.TargetNodeID != 4 {
		t.Errorf("target = %d, want 4 (HttpClient.Get) — the Go receiver var + field type prove it, NOT 6 (CacheClient.Get)", r.TargetNodeID)
	}
}

// TestResolve_FieldType_GoReceiverVarMismatchAbstains pins correct-or-quiet: when the
// qualifier's prefix is NOT the caller's receiver var (nor self/this), rung 2b ABSTAINS.
// Here the caller's receiver is `r` but the call qualifier is `q.client.Get` — an unknown
// handle → no field_type edge is minted.
func TestResolve_FieldType_GoReceiverVarMismatchAbstains(t *testing.T) {
	files := []string{"svc.go", "net.go"}
	langs := []string{"go", "go"}
	fm := BuildFileMap(files, langs)

	nodeIDs := map[string][]int64{
		"handle": {1}, "Service": {10}, "HttpClient": {3}, "Get": {4},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.go": {"handle": {1}, "Service": {10}},
		"net.go": {"HttpClient": {3}, "Get": {4}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", File: "svc.go", Name: "handle", ParentID: 10, ReceiverName: "r"},
		10: {Label: "Struct", File: "svc.go", Name: "Service"},
		3:  {Label: "Struct", File: "net.go", Name: "HttpClient"},
		4:  {Label: "Method", File: "net.go", Name: "Get", ParentID: 3},
	}
	calls := []parser.CallRef{
		// `q` is NOT the receiver var `r` and not self/this → unknown handle.
		{CallerNodeIdx: 0, CalleeName: "Get", CalleeQualified: "q.client.Get", Line: 5, File: "svc.go"},
	}
	callerIDs := []int64{1}

	SetFieldTypeIndex(map[int64]map[string]string{10: {"client": "HttpClient"}})
	defer SetFieldTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	for _, r := range resolved {
		if r.EvidenceType == "field_type" {
			t.Errorf("rung 2b minted a field_type edge for a non-receiver qualifier `q.`: %+v", r)
		}
	}
}
