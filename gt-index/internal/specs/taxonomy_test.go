package specs

import (
	"sort"
	"strings"
	"testing"

	sitter "github.com/smacker/go-tree-sitter"
)

// registeredLanguages returns one entry per registered Spec (the Registry is
// keyed by extension, so several keys share a Spec).
func registeredLanguages(t *testing.T) map[string]*Spec {
	t.Helper()
	byName := map[string]*Spec{}
	for _, s := range Registry {
		byName[s.Name] = s
	}
	if len(byName) == 0 {
		t.Fatal("spec registry is empty")
	}
	return byName
}

func sortedKeys(m map[string]*Spec) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// TestEveryLanguageHasATaxonomy: a grammar with no mapping is a grammar nobody
// looked at, and the matrix in REPORT.md would silently omit it.
func TestEveryLanguageHasATaxonomy(t *testing.T) {
	langs := registeredLanguages(t)
	for _, name := range sortedKeys(langs) {
		if TaxonomyFor(name) == nil {
			t.Errorf("language %q has a spec but no taxonomy mapping", name)
		}
	}
	for lang := range Taxonomies {
		if _, ok := langs[lang]; !ok {
			t.Errorf("taxonomy %q maps a language with no registered spec", lang)
		}
	}
}

// kindStates returns, for one language, the kind -> state it is accounted in.
func kindStates(tx *Taxonomy) (present, asProperty, absentKinds map[string]bool) {
	present = map[string]bool{}
	asProperty = map[string]bool{}
	absentKinds = map[string]bool{}
	for _, k := range tx.ByNodeType {
		present[k] = true
	}
	for _, ck := range tx.ChildKinds {
		present[ck.Kind] = true
	}
	for _, kw := range tx.Keywords {
		present[kw.Kind] = true
	}
	for _, d := range tx.Decls {
		present[d.Kind] = true
	}
	for _, m := range tx.MemberDecls {
		present[m.Kind] = true
	}
	if len(tx.ConstructorNames) > 0 {
		present[KindConstructor] = true
	}
	for _, k := range UniversalKinds {
		present[k] = true
	}
	for k := range tx.AsProperty {
		asProperty[k] = true
	}
	for k := range tx.Absent {
		absentKinds[k] = true
	}
	return
}

// TestTaxonomyCoversEveryKind is the invariant that makes the language x kind
// matrix trustworthy: for every language, every kind in the closed vocabulary
// is either mapped to a grammar node type, recorded as an existing property
// row, or declared absent WITH A REASON. Silence is not one of the options.
func TestTaxonomyCoversEveryKind(t *testing.T) {
	for _, lang := range sortedTaxonomyNames() {
		tx := Taxonomies[lang]
		present, asProperty, absentKinds := kindStates(tx)
		for _, kind := range AllSymbolKinds {
			states := 0
			if present[kind] {
				states++
			}
			if asProperty[kind] {
				states++
			}
			if absentKinds[kind] {
				states++
			}
			switch {
			case states == 0:
				t.Errorf("%s: kind %q is unaccounted: map it, mark it as a property, or declare it absent with a reason", lang, kind)
			case states > 1:
				t.Errorf("%s: kind %q is accounted %d times; each kind belongs to exactly one state", lang, kind, states)
			}
		}
		for kind, reason := range tx.Absent {
			if !IsSymbolKind(kind) {
				t.Errorf("%s: absence declared for %q, which is not in the vocabulary", lang, kind)
			}
			if strings.TrimSpace(reason) == "" {
				t.Errorf("%s: kind %q is declared absent with an empty reason", lang, kind)
			}
		}
		for kind, prop := range tx.AsProperty {
			if !IsSymbolKind(kind) {
				t.Errorf("%s: property mapping for %q, which is not in the vocabulary", lang, kind)
			}
			if strings.TrimSpace(prop) == "" {
				t.Errorf("%s: kind %q maps to an empty property row kind", lang, kind)
			}
		}
	}
}

// TestTaxonomyKindsAreInTheVocabulary refuses a kind invented in one language
// file: the vocabulary is closed so the matrix has a fixed set of columns.
func TestTaxonomyKindsAreInTheVocabulary(t *testing.T) {
	for _, lang := range sortedTaxonomyNames() {
		tx := Taxonomies[lang]
		check := func(where, kind string) {
			if !IsSymbolKind(kind) {
				t.Errorf("%s: %s yields kind %q, which is not in AllSymbolKinds", lang, where, kind)
			}
		}
		for nodeType, kind := range tx.ByNodeType {
			check("ByNodeType["+nodeType+"]", kind)
		}
		for _, ck := range tx.ChildKinds {
			check("ChildKinds["+ck.NodeType+"]", ck.Kind)
		}
		for _, kw := range tx.Keywords {
			check("Keywords["+kw.NodeType+"]", kw.Kind)
		}
		for nodeType, d := range tx.Decls {
			check("Decls["+nodeType+"]", d.Kind)
			if strings.TrimSpace(d.Label) == "" {
				t.Errorf("%s: Decls[%s] has no label", lang, nodeType)
			}
		}
		for nodeType, m := range tx.MemberDecls {
			check("MemberDecls["+nodeType+"]", m.Kind)
			if strings.TrimSpace(m.Label) == "" {
				t.Errorf("%s: MemberDecls[%s] has no label", lang, nodeType)
			}
			if len(m.Types) == 0 {
				t.Errorf("%s: MemberDecls[%s] names no member node types", lang, nodeType)
			}
		}
	}
}

// grammarNodeTypes returns every named node type the compiled grammar can
// produce, read from the tree-sitter symbol table itself.
func grammarNodeTypes(lang *sitter.Language) map[string]bool {
	out := make(map[string]bool, lang.SymbolCount())
	for i := uint32(0); i < lang.SymbolCount(); i++ {
		sym := sitter.Symbol(i)
		if lang.SymbolType(sym) != sitter.SymbolTypeRegular {
			continue
		}
		out[lang.SymbolName(sym)] = true
	}
	return out
}

// TestTaxonomyNodeTypesExistInTheGrammar is the test that stops the taxonomy
// from inventing evidence. Every node type named in a mapping is checked
// against the compiled grammar's own symbol table, so a mapping cannot claim a
// construct the language does not have. A node type that does not exist would
// otherwise be invisible: it would simply never match at parse time, and the
// matrix would report a capability GT does not have.
func TestTaxonomyNodeTypesExistInTheGrammar(t *testing.T) {
	langs := registeredLanguages(t)
	for _, name := range sortedKeys(langs) {
		tx := TaxonomyFor(name)
		if tx == nil {
			continue
		}
		types := grammarNodeTypes(langs[name].Language)
		must := func(where, nodeType string) {
			if nodeType == "" {
				return
			}
			if !types[nodeType] {
				t.Errorf("%s: %s names node type %q, which the compiled grammar cannot produce", name, where, nodeType)
			}
		}
		for nodeType := range tx.ByNodeType {
			must("ByNodeType", nodeType)
		}
		for _, ck := range tx.ChildKinds {
			must("ChildKinds.NodeType", ck.NodeType)
			must("ChildKinds.ChildType", ck.ChildType)
		}
		for _, kw := range tx.Keywords {
			must("Keywords.NodeType", kw.NodeType)
		}
		for nodeType := range tx.Decls {
			must("Decls", nodeType)
		}
		for nodeType, m := range tx.MemberDecls {
			must("MemberDecls", nodeType)
			must("MemberDecls.Of", m.Of)
			for _, mt := range m.Types {
				must("MemberDecls.Types", mt)
			}
		}
		for _, r := range tx.Implements {
			must("Implements.NodeType", r.NodeType)
			must("Implements.ChildType", r.ChildType)
		}
	}
}

// TestNewLabelsNeverCollideWithASymbolLabelTheParserAlreadyAssigns pins the
// property that keeps pre-existing label totals fixed on any repository whose
// languages the parser already emits nodes for: a Decl must not reuse a label
// for a node type the parser ALREADY emits, because that would move rows
// between labels rather than add them.
func TestNewLabelsNeverCollideWithASymbolLabelTheParserAlreadyAssigns(t *testing.T) {
	langs := registeredLanguages(t)
	for _, name := range sortedKeys(langs) {
		tx := TaxonomyFor(name)
		if tx == nil {
			continue
		}
		spec := langs[name]
		for nodeType := range tx.Decls {
			if spec.IsFunctionNode(nodeType) || spec.IsClassNode(nodeType) {
				t.Errorf("%s: Decls[%s] re-declares a node type the parser already emits; annotate it with ByNodeType instead", name, nodeType)
			}
		}
	}
}

// TestTaxonomyEdgeKindsCarryAMechanismAndNeverCertify pins delta row 9's
// acceptance criterion in the vocabulary itself: every taxonomy edge kind
// declares the mechanisms that may produce it, and the tier vocabulary
// available to those mechanisms contains no CERTIFIED.
func TestTaxonomyEdgeKindsCarryAMechanismAndNeverCertify(t *testing.T) {
	for _, kind := range AllTaxonomyEdgeKinds {
		mechs, ok := TaxonomyEdgeMechanisms[kind]
		if !ok || len(mechs) == 0 {
			t.Errorf("edge kind %q declares no mechanism", kind)
			continue
		}
		for _, m := range mechs {
			if !strings.HasPrefix(m, "syntactic_") {
				t.Errorf("edge kind %q mechanism %q does not name a syntactic reading", kind, m)
			}
		}
	}
	for _, tier := range []string{TierCandidate, TierSpeculative} {
		if tier == "CERTIFIED" || tier == "VERIFIED" {
			t.Fatalf("taxonomy tier vocabulary contains %q", tier)
		}
	}
	if len(TaxonomyEdgeMechanisms) != len(AllTaxonomyEdgeKinds) {
		t.Errorf("mechanism table covers %d kinds, vocabulary has %d",
			len(TaxonomyEdgeMechanisms), len(AllTaxonomyEdgeKinds))
	}
}

// TestImplementsIsOnlyClaimedWhereTheGrammarSeparatesIt records, as an
// executable statement, WHICH grammars state interface conformance separately
// from superclass extension. If a language file later adds an Implements rule,
// this test forces the addition to be deliberate.
func TestImplementsIsOnlyClaimedWhereTheGrammarSeparatesIt(t *testing.T) {
	want := map[string]bool{
		"typescript": true, // class_heritage -> implements_clause
		"java":       true, // super_interfaces, separate from superclass
		"groovy":     true, // super_interfaces, separate from superclass
		"php":        true, // class_interface_clause
		"rust":       true, // impl Trait for Type, trait is a named field
	}
	got := map[string]bool{}
	for lang, tx := range Taxonomies {
		if len(tx.Implements) > 0 {
			got[lang] = true
		}
	}
	for lang := range want {
		if !got[lang] {
			t.Errorf("%s separates interface conformance syntactically but claims no IMPLEMENTS rule", lang)
		}
	}
	for lang := range got {
		if !want[lang] {
			t.Errorf("%s claims IMPLEMENTS; add it to the reviewed list only if the grammar separates conformance from extension", lang)
		}
	}
}

// TestContainsWordMatchesOnlyWholeWords guards the keyword refinement: `def`
// must not match inside `defmodule`, and `get` must not match inside `getUser`.
func TestContainsWordMatchesOnlyWholeWords(t *testing.T) {
	cases := []struct {
		text, word string
		want       bool
	}{
		{"defmodule Foo do", "def", false},
		{"defmodule Foo do", "defmodule", true},
		{"  get value() {", "get", true},
		{"getValue() {", "get", false},
		{"@Override public void f() {", "@Override", true},
		{"public void overrideCount() {", "override", false},
		{"public override void F() {", "override", true},
		{"enum class Color {", "enum", true},
		{"class Enumerable {", "enum", false},
		{"", "override", false},
		{"override", "override", true},
	}
	for _, c := range cases {
		if got := ContainsWord(c.text, c.word); got != c.want {
			t.Errorf("ContainsWord(%q, %q) = %v, want %v", c.text, c.word, got, c.want)
		}
	}
}

func sortedTaxonomyNames() []string {
	out := make([]string, 0, len(Taxonomies))
	for k := range Taxonomies {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
