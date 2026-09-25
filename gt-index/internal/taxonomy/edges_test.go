package taxonomy

import (
	"encoding/json"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

func node(label, name, file string) *store.Node {
	return &store.Node{Label: label, Name: name, FilePath: file, StartLine: 1}
}

func idsFor(n int) []int64 {
	out := make([]int64, n)
	for i := range out {
		out[i] = int64(i + 1)
	}
	return out
}

func edgesByKind(rows []*store.Edge, kind string) []*store.Edge {
	var out []*store.Edge
	for _, e := range rows {
		if e.Type == kind {
			out = append(out, e)
		}
	}
	return out
}

// TestNoTaxonomyEdgeIsEverCertified is the acceptance criterion of delta row 9
// stated as a test over the produced rows, not over the code that produces
// them: whatever the mechanism, whatever the ambiguity, no row claims a tier
// its evidence did not earn.
func TestNoTaxonomyEdgeIsEverCertified(t *testing.T) {
	nodes := []*store.Node{
		node("Class", "Circle", "a.ts"),
		node("Interface", "Shape", "a.ts"),
		node("Method", "area", "a.ts"),
		node("Method", "area", "b.ts"),
	}
	nodes[2].ReturnType = "Shape"
	props := []parser.PropertyRef{
		{NodeIdx: 0, Kind: parser.PropImplementsType, Value: "Shape|" + specs.MechImplementsClause, Line: 1},
		{NodeIdx: 2, Kind: parser.PropOverrideMarker, Value: specs.MechOverrideMarker, Line: 3},
		{NodeIdx: 2, Kind: propParam, Value: "s:Shape [required]", Line: 3},
	}
	rows := DeriveEdges(nodes, idsFor(len(nodes)), props)
	if len(rows) == 0 {
		t.Fatal("expected taxonomy edges")
	}
	for _, e := range rows {
		switch e.TrustTier {
		case specs.TierCandidate, specs.TierSpeculative:
		default:
			t.Errorf("%s edge carries tier %q; only CANDIDATE and SPECULATIVE are reachable",
				e.Type, e.TrustTier)
		}
		if e.VerificationStatus != "unverified" {
			t.Errorf("%s edge is %q; a syntactic reading is never verified", e.Type, e.VerificationStatus)
		}
		if e.Confidence >= 1.0 {
			t.Errorf("%s edge has confidence %v; a syntactic reading is not certain", e.Type, e.Confidence)
		}
		if !isTaxonomyMechanism(e.Type, e.ResolutionMethod) {
			t.Errorf("%s edge carries mechanism %q, which its kind does not grant", e.Type, e.ResolutionMethod)
		}
	}
}

// TestUniqueNameIsCandidateAmbiguousIsSpeculative pins the tier rule itself.
func TestUniqueNameIsCandidateAmbiguousIsSpeculative(t *testing.T) {
	nodes := []*store.Node{
		node("Class", "Circle", "a.ts"),
		node("Interface", "Shape", "a.ts"),
	}
	props := []parser.PropertyRef{
		{NodeIdx: 0, Kind: parser.PropImplementsType, Value: "Shape|" + specs.MechImplementsClause, Line: 1},
	}
	rows := edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), props), specs.EdgeDeclaredImplements)
	if len(rows) != 1 {
		t.Fatalf("unique name: %d DECLARED_IMPLEMENTS rows, want 1", len(rows))
	}
	if rows[0].TrustTier != specs.TierCandidate || rows[0].CandidateCount != 1 {
		t.Errorf("unique name: tier=%s count=%d, want CANDIDATE/1", rows[0].TrustTier, rows[0].CandidateCount)
	}

	// Two declarations carry the name. gnx's schema would force a pick here.
	nodes = append(nodes, node("Interface", "Shape", "vendor/b.ts"))
	rows = edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), props), specs.EdgeDeclaredImplements)
	if len(rows) != 2 {
		t.Fatalf("ambiguous name: %d DECLARED_IMPLEMENTS rows, want 2 (both retained)", len(rows))
	}
	for _, e := range rows {
		if e.TrustTier != specs.TierSpeculative {
			t.Errorf("ambiguous name: tier=%s, want SPECULATIVE", e.TrustTier)
		}
		if e.CandidateCount != 2 {
			t.Errorf("ambiguous name: candidate_count=%d, want 2", e.CandidateCount)
		}
	}
}

// TestNameBindingNeverCrossesALanguageFamily pins the polyglot boundary a
// bare-name resolution must respect: a TypeScript `implements Greeter` can
// never bind the Go interface named Greeter — the same class of false edge
// the resolver's IMPLEMENTS pass was caught emitting. Every name-resolving
// kind obeys it.
func TestNameBindingNeverCrossesALanguageFamily(t *testing.T) {
	langNode := func(label, name, file, lang string) *store.Node {
		n := node(label, name, file)
		n.Language = lang
		return n
	}

	// DECLARED_IMPLEMENTS: a foreign Greeter is not a candidate at all, so
	// it cannot even inflate the ambiguity count.
	nodes := []*store.Node{
		langNode("Class", "Widget", "a.ts", "typescript"),
		langNode("Interface", "Greeter", "g.go", "go"),
		langNode("Interface", "Greeter", "b.ts", "typescript"),
	}
	props := []parser.PropertyRef{
		{NodeIdx: 0, Kind: parser.PropImplementsType, Value: "Greeter|" + specs.MechImplementsClause, Line: 1},
	}
	rows := edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), props), specs.EdgeDeclaredImplements)
	if len(rows) != 1 || rows[0].TargetID != 3 {
		t.Fatalf("cross-language implements: %d rows, want exactly 1 targeting the TypeScript Greeter", len(rows))
	}
	if rows[0].TrustTier != specs.TierCandidate || rows[0].CandidateCount != 1 {
		t.Errorf("foreign candidate leaked into the count: tier=%s count=%d, want CANDIDATE/1",
			rows[0].TrustTier, rows[0].CandidateCount)
	}

	// And with ONLY a foreign declaration carrying the name, the clause
	// abstains rather than binding across the boundary.
	rows = edgesByKind(DeriveEdges(nodes[:2], idsFor(2), props), specs.EdgeDeclaredImplements)
	if len(rows) != 0 {
		t.Errorf("implements whose only candidate is foreign emitted %d rows, want 0", len(rows))
	}

	// JVM family members interoperate by name: Java code may implement a
	// Kotlin interface.
	nodes = []*store.Node{
		langNode("Class", "Widget", "A.java", "java"),
		langNode("Interface", "Greeter", "B.kt", "kotlin"),
	}
	rows = edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), props), specs.EdgeDeclaredImplements)
	if len(rows) != 1 || rows[0].TargetID != 2 {
		t.Fatalf("same-family implements: %d rows, want 1 -> the Kotlin Greeter", len(rows))
	}

	// PARAM_TYPE: a Python annotation cannot resolve to a same-named Go
	// struct.
	nodes = []*store.Node{
		langNode("Function", "handle", "h.py", "python"),
		langNode("Struct", "Config", "c.go", "go"),
	}
	props = []parser.PropertyRef{
		{NodeIdx: 0, Kind: propParam, Value: "cfg:Config [required]", Line: 1},
	}
	if rows := edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), props), specs.EdgeParamType); len(rows) != 0 {
		t.Errorf("cross-language PARAM_TYPE emitted %d rows, want 0", len(rows))
	}

	// RETURNS_TYPE: a Go return type cannot resolve to a same-named TS class.
	fn := langNode("Function", "build", "x.go", "go")
	fn.ReturnType = "Result"
	nodes = []*store.Node{fn, langNode("Class", "Result", "r.ts", "typescript")}
	if rows := edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), nil), specs.EdgeReturnsType); len(rows) != 0 {
		t.Errorf("cross-language RETURNS_TYPE emitted %d rows, want 0", len(rows))
	}

	// DECORATES: a Python decorator name cannot bind a same-named Go
	// function.
	nodes = []*store.Node{
		langNode("Class", "Cfg", "a.py", "python"),
		langNode("Function", "registry", "d.go", "go"),
	}
	props = []parser.PropertyRef{
		{NodeIdx: 0, Kind: propClassDecorator, Value: "@registry", Line: 1},
	}
	if rows := edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), props), specs.EdgeDecorates); len(rows) != 0 {
		t.Errorf("cross-language DECORATES emitted %d rows, want 0", len(rows))
	}

	// OVERRIDES: a Kotlin method marker cannot claim an override of a
	// same-named Rust method.
	nodes = []*store.Node{
		langNode("Method", "draw", "A.kt", "kotlin"),
		langNode("Method", "draw", "b.rs", "rust"),
	}
	props = []parser.PropertyRef{
		{NodeIdx: 0, Kind: parser.PropOverrideMarker, Value: specs.MechOverrideMarker, Line: 2},
	}
	if rows := edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), props), specs.EdgeMethodOverrides); len(rows) != 0 {
		t.Errorf("cross-language METHOD_OVERRIDES emitted %d rows, want 0", len(rows))
	}
}

// TestUnknownLanguageNeverFilters: a node whose language metadata is missing
// keeps its historical permissive behavior — the filter may only drop a
// declaration it KNOWS belongs to another family.
func TestUnknownLanguageNeverFilters(t *testing.T) {
	src := node("Class", "Circle", "a.ts")
	src.Language = "typescript"
	cand := node("Interface", "Shape", "b.ts") // Language unset
	nodes := []*store.Node{src, cand}
	props := []parser.PropertyRef{
		{NodeIdx: 0, Kind: parser.PropImplementsType, Value: "Shape|" + specs.MechImplementsClause, Line: 1},
	}
	rows := edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), props), specs.EdgeDeclaredImplements)
	if len(rows) != 1 || rows[0].TargetID != 2 {
		t.Fatalf("unknown-language candidate filtered: %d rows, want 1", len(rows))
	}
}

// TestOverSizedCandidateSetKeepsTheTrueCount: the retained list is capped, but
// the recorded count is the real one and the truncation is stated, so a reader
// cannot mistake a capped set for a small one.
func TestOverSizedCandidateSetKeepsTheTrueCount(t *testing.T) {
	nodes := []*store.Node{node("Class", "Circle", "a.ts")}
	total := specs.MaxTaxonomyCandidates + 5
	for i := 0; i < total; i++ {
		nodes = append(nodes, node("Interface", "Shape", "v.ts"))
	}
	props := []parser.PropertyRef{
		{NodeIdx: 0, Kind: parser.PropImplementsType, Value: "Shape|" + specs.MechImplementsClause, Line: 1},
	}
	rows := edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), props), specs.EdgeDeclaredImplements)
	if len(rows) != specs.MaxTaxonomyCandidates {
		t.Fatalf("retained %d rows, want the cap %d", len(rows), specs.MaxTaxonomyCandidates)
	}
	for _, e := range rows {
		if e.CandidateCount != total {
			t.Errorf("candidate_count=%d, want the true total %d", e.CandidateCount, total)
		}
		var meta map[string]any
		if err := json.Unmarshal([]byte(e.Metadata), &meta); err != nil {
			t.Fatalf("metadata is not JSON: %v", err)
		}
		if meta["abstention_reason"] != "candidate_set_truncated" {
			t.Errorf("metadata does not record the truncation: %v", meta)
		}
	}
}

// TestAMechanismTheKindDoesNotGrantIsDropped: a fact row with a mechanism
// outside the edge kind's vocabulary produces nothing, rather than an edge
// with a mechanism nobody sanctioned.
func TestAMechanismTheKindDoesNotGrantIsDropped(t *testing.T) {
	nodes := []*store.Node{node("Class", "Circle", "a.ts"), node("Interface", "Shape", "a.ts")}
	props := []parser.PropertyRef{
		{NodeIdx: 0, Kind: parser.PropImplementsType, Value: "Shape|guessed_from_a_name_prefix", Line: 1},
	}
	if rows := DeriveEdges(nodes, idsFor(len(nodes)), props); len(rows) != 0 {
		t.Errorf("expected no edges for an ungranted mechanism, got %d", len(rows))
	}
}

// TestUnresolvableNamesAbstain: a return type that names no declaration in the
// repository produces no edge. Abstention is the correct answer, not a
// dangling row.
func TestUnresolvableNamesAbstain(t *testing.T) {
	fn := node("Function", "build", "a.ts")
	fn.ReturnType = "number"
	nodes := []*store.Node{fn, node("Class", "Circle", "a.ts")}
	rows := edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), nil), specs.EdgeReturnsType)
	if len(rows) != 0 {
		t.Errorf("primitive return type produced %d edges, want 0", len(rows))
	}

	fn.ReturnType = "Circle"
	rows = edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), nil), specs.EdgeReturnsType)
	if len(rows) != 1 || rows[0].TargetID != 2 {
		t.Errorf("declared return type produced %d edges, want 1 pointing at Circle", len(rows))
	}
}

// TestOverridesNeverPointsAtItself guards the one self-reference this pass can
// produce: the marker is on a method, and the index of methods by name
// contains that method.
func TestOverridesNeverPointsAtItself(t *testing.T) {
	nodes := []*store.Node{node("Method", "area", "a.ts")}
	props := []parser.PropertyRef{
		{NodeIdx: 0, Kind: parser.PropOverrideMarker, Value: specs.MechOverrideMarker, Line: 2},
	}
	if rows := DeriveEdges(nodes, idsFor(len(nodes)), props); len(rows) != 0 {
		t.Errorf("a lone marked method produced %d OVERRIDES edges, want 0", len(rows))
	}
}

// TestDecoratesPointsFromTheDecorator fixes the direction, which is the kind
// of thing that is quietly wrong forever if no test states it.
func TestDecoratesPointsFromTheDecorator(t *testing.T) {
	nodes := []*store.Node{
		node("Class", "Config", "a.py"),
		node("Function", "dataclass", "d.py"),
	}
	props := []parser.PropertyRef{
		{NodeIdx: 0, Kind: propClassDecorator, Value: "@dataclass", Line: 1},
	}
	rows := edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), props), specs.EdgeDecorates)
	if len(rows) != 1 {
		t.Fatalf("%d DECORATES rows, want 1", len(rows))
	}
	if rows[0].SourceID != 2 || rows[0].TargetID != 1 {
		t.Errorf("DECORATES %d->%d, want 2->1 (decorator decorates the declaration)",
			rows[0].SourceID, rows[0].TargetID)
	}
}

// An external decorator has no in-repo callable — the `Decorator` occurrence
// node the parser parents to the declaration is the honest DECORATES source.
func TestDecoratesExternalUsesOccurrenceNode(t *testing.T) {
	nodes := []*store.Node{
		node("Function", "handler", "a.py"),
		{Label: "Decorator", Name: "route", QualifiedName: "app.route",
			FilePath: "a.py", StartLine: 1, ParentID: 1},
	}
	props := []parser.PropertyRef{
		{NodeIdx: 0, Kind: propFunctionDecorator, Value: "@app.route('/x')", Line: 1},
	}
	rows := edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), props), specs.EdgeDecorates)
	if len(rows) != 1 {
		t.Fatalf("%d DECORATES rows, want 1", len(rows))
	}
	if rows[0].SourceID != 2 || rows[0].TargetID != 1 {
		t.Errorf("DECORATES %d->%d, want 2->1 (occurrence node is the source)",
			rows[0].SourceID, rows[0].TargetID)
	}
	if rows[0].TrustTier == "CERTIFIED" {
		t.Errorf("external-decorator edge claimed CERTIFIED: %+v", rows[0])
	}
}

// When an in-repo callable carries the decorator's name it wins; the
// occurrence node is a fallback, never an extra edge.
func TestDecoratesPrefersInRepoCallable(t *testing.T) {
	nodes := []*store.Node{
		node("Function", "handler", "a.py"),
		{Label: "Decorator", Name: "logged", QualifiedName: "logged",
			FilePath: "a.py", StartLine: 1, ParentID: 1},
		node("Function", "logged", "deco.py"),
	}
	props := []parser.PropertyRef{
		{NodeIdx: 0, Kind: propFunctionDecorator, Value: "@logged", Line: 1},
	}
	rows := edgesByKind(DeriveEdges(nodes, idsFor(len(nodes)), props), specs.EdgeDecorates)
	if len(rows) != 1 {
		t.Fatalf("%d DECORATES rows, want exactly 1 (callable wins, no double edge)", len(rows))
	}
	if rows[0].SourceID != 3 || rows[0].TargetID != 1 {
		t.Errorf("DECORATES %d->%d, want 3->1 (in-repo callable)", rows[0].SourceID, rows[0].TargetID)
	}
}

func TestRequiredNamedEdgesUseSyntacticEvidenceAndNeverCertify(t *testing.T) {
	nodes := []*store.Node{
		node("Class", "Dependency", "dep.py"),
		node("Class", "Service", "svc.py"),
		node("Method", "__init__", "svc.py"),
		node("Method", "run", "svc.py"),
		node("Method", "run", "base.py"),
	}
	// Production main has translated the parser parent ordinal to Service's
	// database ID (idsFor assigns Service ID 2) before DeriveEdges runs.
	nodes[2].ParentID = 2
	nodes[3].ParentID = 2
	props := []parser.PropertyRef{
		{NodeIdx: 2, Kind: propParam, Value: "dep:Dependency [required]", Line: 3},
		{NodeIdx: 3, Kind: propFieldRead, Value: "reads: self.value", Line: 7},
		{NodeIdx: 3, Kind: parser.PropOverrideMarker, Value: specs.MechOverrideMarker, Line: 6},
	}
	rows := DeriveEdges(nodes, idsFor(len(nodes)), props)

	for _, kind := range []string{specs.EdgeAccesses, specs.EdgeInjects, specs.EdgeMethodOverrides} {
		got := edgesByKind(rows, kind)
		if len(got) != 1 {
			t.Fatalf("%s rows=%d, want 1", kind, len(got))
		}
		if got[0].TrustTier == "CERTIFIED" || got[0].ResolutionMethod == "" {
			t.Fatalf("%s has invalid tier/mechanism: %+v", kind, got[0])
		}
	}
}

func TestOwningTypeIDUsesPrecomputedDatabaseIdentity(t *testing.T) {
	service := node("Class", "Service", "svc.py")
	method := node("Method", "run", "svc.py")
	method.ParentID = 41

	got := owningTypeID(method, map[int64]*store.Node{41: service})
	if got != 41 {
		t.Fatalf("owningTypeID() = %d, want sparse database ID 41", got)
	}
}

// TestNoTaxonomyEdgeReusesAPreExistingKind is the additivity guard for delta
// row 9. The list below was MEASURED on 2026-09-03 by enumerating every
// upper-case string literal in internal/resolver, internal/store, internal/
// closure, internal/process, internal/community, internal/cochange and
// cmd/gt-index. IMPLEMENTS is on it: relationships.go already writes that kind
// from a line-based regex at confidence 1.0, which is exactly why the
// tree-sitter reading is published as DECLARED_IMPLEMENTS beside it rather
// than merged into it.
func TestNoTaxonomyEdgeReusesAPreExistingKind(t *testing.T) {
	preExisting := map[string]bool{
		"API_CALL": true, "CALLS": true, "CANDIDATE": true,
		"CANDIDATE_TARGET": true, "COMPOSES": true, "CONTAINS": true,
		"CO_SERIALIZES": true, "DATA_FLOW": true, "EXTENDS": true,
		"HANDLES_ROUTE": true, "HAS_CALLSITE": true,
		"HAS_COMPLETENESS_FACT": true, "HAS_DERIVATION_FACT": true,
		"HAS_UNRESOLVED_FACT": true, "IMPLEMENTS": true, "IMPORTS": true,
		"PRECEDES": true, "RAISES": true, "READS": true, "RE_EXPORTS": true,
		"SELECTED_TARGET": true, "TEST_CALLS": true, "WRITES": true,
	}
	for _, kind := range specs.AllTaxonomyEdgeKinds {
		if preExisting[kind] {
			t.Errorf("taxonomy edge kind %q collides with a kind GT already writes; "+
				"reusing it would move a pre-existing total and merge two mechanisms "+
				"of different strength under one name", kind)
		}
	}
}

func TestTypeReferenceNameRefusesWhatItCannotRead(t *testing.T) {
	cases := []struct{ in, want string }{
		{"Circle", "Circle"},
		{": Circle", "Circle"},  // tree-sitter-typescript return_type
		{"-> Circle", "Circle"}, // python / rust return_type
		{"(int, error)", ""},    // a Go tuple result names no single type
		{"*Point", "Point"},
		{"foo::Bar<T>", "Bar"},
		{"a.b.C", "C"},
		{"Promise<Circle>", "Promise"},
		{"", ""},
		{"number[]", "number"},
		{"{ x: number }", ""},
		{"A | B", "A"},
	}
	for _, c := range cases {
		if got := typeReferenceName(c.in); got != c.want {
			t.Errorf("typeReferenceName(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestParamTypeNameReadsOnlyAnnotatedParams(t *testing.T) {
	cases := []struct{ in, want string }{
		{"r:Circle [required]", "Circle"},
		{"n:number opt=1", "number"},
		{"self [required]", ""},
		{"", ""},
	}
	for _, c := range cases {
		if got := paramTypeName(c.in); got != c.want {
			t.Errorf("paramTypeName(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestDecoratorNameStripsTheApplication(t *testing.T) {
	cases := []struct{ in, want string }{
		{"@dataclass", "dataclass"},
		{"@pytest.fixture(scope=\"module\")", "fixture"},
		{"@Override", "Override"},
		{"@", ""},
	}
	for _, c := range cases {
		if got := decoratorName(c.in); got != c.want {
			t.Errorf("decoratorName(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
