// Symbol taxonomy and edge kinds (final_hardening item 11, delta rows 8 and 9).
//
// GT keeps ONE node model. This file does not add node tables; it adds a
// vocabulary that every language mapping must account for, so a reader can ask
// "does this grammar have a trait?" and get an answer that is either a
// tree-sitter node type or a written reason, never silence.
//
// Three states, and every kind is in exactly one of them per language:
//
//	present     — the grammar has the construct and names it with a node type
//	as-property — GT already records the fact as a typed property row, so a
//	              second representation would duplicate it
//	absent      — the grammar has no syntax for it, with the reason recorded
//
// Nothing here promotes evidence. A kind is a syntactic reading of a node type
// the grammar produced; it carries the mechanism that produced it and never a
// trust tier above what that mechanism grants.
package specs

// ── Symbol kinds ──────────────────────────────────────────────────────────

const (
	KindFunction    = "function"
	KindMethod      = "method"
	KindConstructor = "constructor"
	KindAccessor    = "accessor"
	KindClass       = "class"
	KindInterface   = "interface"
	KindStruct      = "struct"
	KindEnum        = "enum"
	KindEnumMember  = "enum_member"
	KindTrait       = "trait"
	KindImpl        = "impl"
	KindTypeAlias   = "type_alias"
	KindProtocol    = "protocol"
	KindRecord      = "record"
	KindUnion       = "union"
	KindNamespace   = "namespace"
	KindModule      = "module"
	KindMacro       = "macro"
	KindConstant    = "constant"
	KindAnnotation  = "annotation"
	KindDecorator   = "decorator"
	KindField       = "field"
	KindFile        = "file"
	KindTable       = "table"
	KindMessage     = "message"
	KindService     = "service"
	KindRule        = "rule"
	KindElement     = "element"
	KindSection     = "section"
	KindResource    = "resource"
)

// AllSymbolKinds is the closed vocabulary. Every language mapping must place
// every one of these in exactly one of ByNodeType/Decls, AsProperty or Absent;
// TestTaxonomyCoversEveryKind enforces it.
var AllSymbolKinds = []string{
	KindFunction, KindMethod, KindConstructor, KindAccessor,
	KindClass, KindInterface, KindStruct, KindEnum, KindEnumMember,
	KindTrait, KindImpl, KindTypeAlias, KindProtocol, KindRecord, KindUnion,
	KindNamespace, KindModule, KindMacro, KindConstant,
	KindAnnotation, KindDecorator, KindField, KindFile,
	KindTable, KindMessage, KindService, KindRule, KindElement, KindSection,
	KindResource,
}

// IsSymbolKind reports whether kind is in the closed vocabulary.
func IsSymbolKind(kind string) bool {
	for _, k := range AllSymbolKinds {
		if k == kind {
			return true
		}
	}
	return false
}

// ── Edge kinds ────────────────────────────────────────────────────────────

// Taxonomy edge kinds. These are ADDITIVE: no pre-existing edge kind changes
// meaning or volume. Each is emitted only where the grammar states the fact
// syntactically, and each carries the mechanism below plus a trust tier that
// the mechanism grants — never CERTIFIED.
const (
	EdgeImplements   = "IMPLEMENTS"
	EdgeOverrides    = "OVERRIDES"
	EdgeDecorates    = "DECORATES"
	EdgeReturnsType  = "RETURNS_TYPE"
	EdgeParamType    = "PARAM_TYPE"
	EdgeInstantiates = "INSTANTIATES"
)

// AllTaxonomyEdgeKinds is the closed set of edge kinds this item introduces.
var AllTaxonomyEdgeKinds = []string{
	EdgeImplements, EdgeOverrides, EdgeDecorates,
	EdgeReturnsType, EdgeParamType, EdgeInstantiates,
}

// Mechanisms. The mechanism names the syntax that produced the edge, so a
// reader can judge the claim without re-reading the source.
const (
	MechImplementsClause = "syntactic_implements_clause"
	MechImplTrait        = "syntactic_impl_trait"
	MechOverrideMarker   = "syntactic_override_marker"
	MechDecoratorApplied = "syntactic_decorator_applied"
	MechReturnAnnotation = "syntactic_return_annotation"
	MechParamAnnotation  = "syntactic_param_annotation"
	MechConstructorCall  = "syntactic_constructor_call"
)

// TaxonomyEdgeMechanisms maps each taxonomy edge kind to the mechanisms that
// may produce it. A row with any other mechanism is a bug, not a weaker fact.
var TaxonomyEdgeMechanisms = map[string][]string{
	EdgeImplements:   {MechImplementsClause, MechImplTrait},
	EdgeOverrides:    {MechOverrideMarker},
	EdgeDecorates:    {MechDecoratorApplied},
	EdgeReturnsType:  {MechReturnAnnotation},
	EdgeParamType:    {MechParamAnnotation},
	EdgeInstantiates: {MechConstructorCall},
}

// Trust tiers a taxonomy edge may carry. A syntactic mechanism proves that the
// SOURCE TEXT names something; it does not prove which declaration that name
// binds to. So the highest tier available here is CANDIDATE (exactly one
// declaration in the repository carries the name) and the fallback is
// SPECULATIVE (several do, and all are retained). CERTIFIED is unreachable by
// construction — see TestTaxonomyEdgesAreNeverCertified.
const (
	TierCandidate   = "CANDIDATE"
	TierSpeculative = "SPECULATIVE"
)

// MaxTaxonomyCandidates bounds the retained candidate set for one ambiguous
// name. Over-approximation stays visible; it does not become unbounded.
const MaxTaxonomyCandidates = 8

// ── Per-language mapping ──────────────────────────────────────────────────

// Decl describes a declaration the parser does NOT already emit as a node and
// that the taxonomy adds. Label is a NEW node label: pre-existing labels
// (Function, Method, Class, Interface, File) are never re-assigned, so
// pre-existing label totals cannot move.
type Decl struct {
	Kind      string // symbol kind, from AllSymbolKinds
	Label     string // new node label
	NameField string // tree-sitter field holding the name; "" = first identifier
}

// MemberDecl emits one node per member of a declaration body — enum variants
// are the motivating case. It is keyed by the PARENT declaration node type and
// applies whether or not GT already emits a node for that parent, so an
// existing label never moves. Members are parented to the declaration, so the
// pre-existing CONTAINS edge already describes the relationship.
type MemberDecl struct {
	Kind      string   // symbol kind for a member
	Label     string   // new node label for a member
	Of        string   // child node type holding the members (e.g. "enum_body")
	Types     []string // member node types inside Of
	NameField string   // field holding the member name; "" = the node's own text
}

// KeywordKind refines a kind when the declaration header carries a keyword the
// node type alone does not distinguish — Kotlin `enum class`, Scala `case
// class`, C++ `struct` vs `class` share a node type in some grammars.
type KeywordKind struct {
	NodeType string // tree-sitter node type this refinement applies to
	Keyword  string // whole word that must appear in the declaration header
	Kind     string // kind to use instead
}

// ImplementsRule names the child node that holds a syntactic implements
// clause. Only grammars that SEPARATE interface conformance from superclass
// extension get a rule: where one clause carries both (C# base_list, Kotlin
// delegation_specifier, Python argument_list) the fact is not syntactic and
// the language declares IMPLEMENTS unavailable instead of guessing.
type ImplementsRule struct {
	NodeType  string // the declaration node (e.g. "class_declaration")
	ChildType string // descendant node holding the interface names
	TraitFrom string // when set, read the trait from this FIELD instead (Rust impl)
	TypeField string // with TraitFrom: the field holding the implementing type
}

// ChildKind refines a kind from a node the grammar nests inside the
// declaration — Go's `type_declaration` wraps `struct_type`, `interface_type`
// or `type_alias`, and the nested node is the exact fact. Structural, so it
// cannot be fooled by a keyword appearing in a comment or a string.
type ChildKind struct {
	NodeType  string // the declaration node type
	ChildType string // descendant node type, searched to MaxChildKindDepth
	Kind      string // kind to use instead
}

// MaxChildKindDepth bounds the descendant search a ChildKind performs.
const MaxChildKindDepth = 3

// Taxonomy is one language's mapping. Files are one per language family.
type Taxonomy struct {
	Lang string

	// ByNodeType assigns a kind to nodes the parser ALREADY emits. It changes
	// no label and no count; it only says what the node is.
	ByNodeType map[string]string

	// ChildKinds refine ByNodeType structurally, from a nested node type.
	// They are applied BEFORE Keywords because they cannot misread text.
	ChildKinds []ChildKind

	// Keywords refine ByNodeType using the declaration header.
	Keywords []KeywordKind

	// ConstructorNames are names that make a function/method a constructor.
	ConstructorNames []string

	// Decls are NEW nodes with NEW labels for declarations GT emitted nothing
	// for. Adding one cannot change a pre-existing label's total.
	Decls map[string]Decl

	// MemberDecls emit member nodes, keyed by parent declaration node type.
	MemberDecls map[string]MemberDecl

	// Implements are the syntactic interface-conformance clauses.
	Implements []ImplementsRule

	// OverrideMarkers are whole words in a method header that state the method
	// overrides a supertype method (`override`, `@Override`).
	OverrideMarkers []string

	// AsProperty records kinds GT already stores as a typed property row,
	// keyed kind -> property row kind.
	AsProperty map[string]string

	// Absent records kinds the grammar has no syntax for, keyed kind -> reason.
	Absent map[string]string
}

// Taxonomies is the registry, keyed by Spec.Name.
var Taxonomies = map[string]*Taxonomy{}

// RegisterTaxonomy adds one language mapping.
func RegisterTaxonomy(t *Taxonomy) {
	Taxonomies[t.Lang] = t
}

// TaxonomyFor returns the mapping for a language, or nil.
func TaxonomyFor(lang string) *Taxonomy {
	return Taxonomies[lang]
}

// KindForNodeType returns the kind for a node type the parser already emits,
// refined structurally and then by header keyword, plus whether a kind was
// found at all. header is the declaration text up to the first `{` or newline;
// hasChild reports whether the declaration nests a given node type. Pass "" and
// nil to skip refinement.
func (t *Taxonomy) KindForNodeType(nodeType, header string, hasChild func(string) bool) (string, bool) {
	kind, ok := t.ByNodeType[nodeType]
	if !ok {
		return "", false
	}
	// Structural refinement first: a nested node type is the grammar's own
	// statement of what the declaration is, and cannot be misread.
	if hasChild != nil {
		for _, ck := range t.ChildKinds {
			if ck.NodeType == nodeType && hasChild(ck.ChildType) {
				return ck.Kind, true
			}
		}
	}
	for _, kw := range t.Keywords {
		if kw.NodeType == nodeType && ContainsWord(header, kw.Keyword) {
			return kw.Kind, true
		}
	}
	return kind, true
}

// IsConstructorName reports whether a callable name is this language's
// constructor spelling.
func (t *Taxonomy) IsConstructorName(name string) bool {
	for _, c := range t.ConstructorNames {
		if c == name {
			return true
		}
	}
	return false
}

// HasOverrideMarker reports whether a declaration header carries an override
// marker for this language.
func (t *Taxonomy) HasOverrideMarker(header string) bool {
	for _, m := range t.OverrideMarkers {
		if ContainsWord(header, m) {
			return true
		}
	}
	return false
}

// ContainsWord reports whether word occurs in text delimited by characters
// that cannot be part of an identifier. `@Override` is matched by treating a
// leading '@' as part of the word.
func ContainsWord(text, word string) bool {
	if word == "" || len(word) > len(text) {
		return false
	}
	for i := 0; i+len(word) <= len(text); i++ {
		if text[i:i+len(word)] != word {
			continue
		}
		if i > 0 && isWordByte(text[i-1]) {
			continue
		}
		if j := i + len(word); j < len(text) && isWordByte(text[j]) {
			continue
		}
		return true
	}
	return false
}

func isWordByte(b byte) bool {
	return b == '_' || b == '$' ||
		(b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9')
}

// ── Mapping-construction helpers ─────────────────────────────────────────
//
// These keep a language file small enough to read in one screen while still
// stating every absence.

// absent marks kinds absent for one shared reason.
func absent(reason string, kinds ...string) map[string]string {
	out := make(map[string]string, len(kinds))
	for _, k := range kinds {
		out[k] = reason
	}
	return out
}

// mergeReasons merges reason maps left to right; later entries win, so a
// specific reason can override a blanket one.
func mergeReasons(maps ...map[string]string) map[string]string {
	out := map[string]string{}
	for _, m := range maps {
		for k, v := range m {
			out[k] = v
		}
	}
	return out
}

// Reasons shared across languages. Naming them keeps the absences honest: a
// reason is a claim about the grammar, and a repeated claim should be one
// string, not thirty paraphrases.
const (
	ReasonNoConstruct   = "the grammar has no declaration for this construct"
	ReasonNotCode       = "this is a data or markup grammar with no code declarations"
	ReasonNoTypeSyntax  = "the grammar declares no types, so this kind cannot appear"
	ReasonNoModuleDecl  = "the grammar has no module or namespace declaration"
	ReasonMarkupOnly    = "the grammar describes markup, not declarations"
	ReasonPropertyOnly  = "GT already records this as a typed property row"
	ReasonNoDecorator   = "the grammar has no decorator or annotation syntax"
	ReasonNoAccessor    = "the grammar has no accessor declaration distinct from a function"
	ReasonNoConstructor = "the grammar has no constructor declaration distinct from a function"
)

// UniversalKinds are satisfied by GT's node model for every language and are
// therefore not repeated in the thirty language files. KindFile is the File
// node the walker emits (and the synthetic anchor for module-linking files).
var UniversalKinds = []string{KindFile}

// noDataKinds states, for a code grammar, that the data-definition kinds have
// no declaration form. Written as a call so each language file names it.
func noDataKinds() map[string]string {
	return absent(ReasonNotCode, KindTable, KindMessage, KindService, KindResource)
}

// noMarkupKinds states, for a non-markup grammar, that the markup kinds have
// no declaration form.
func noMarkupKinds() map[string]string {
	return absent(ReasonMarkupOnly, KindRule, KindElement, KindSection)
}

// noCodeKinds states, for a data or markup grammar, that every code-level kind
// is absent. Data grammars use it and then add back what they do have.
func noCodeKinds() map[string]string {
	return absent(ReasonNotCode,
		KindFunction, KindMethod, KindConstructor, KindAccessor,
		KindClass, KindInterface, KindStruct, KindEnum, KindEnumMember,
		KindTrait, KindImpl, KindTypeAlias, KindProtocol, KindRecord, KindUnion,
		KindNamespace, KindModule, KindMacro, KindConstant,
		KindAnnotation, KindDecorator, KindField,
	)
}
