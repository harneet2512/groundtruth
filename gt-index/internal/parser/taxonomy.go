package parser

// Symbol taxonomy emission (final_hardening item 11, delta rows 8 and 9).
//
// Two additive outputs, both driven entirely by the per-language mappings in
// internal/specs. Nothing here knows a language name.
//
//  1. `symbol_kind` — one typed property row per symbol node, saying WHAT the
//     node is (struct, trait, impl, enum, type_alias, namespace, …). No label
//     changes and no node moves, so every pre-existing label total is fixed by
//     construction.
//
//  2. New declaration nodes, with NEW labels, for declarations the parser
//     emitted nothing for at all — a TypeScript enum, a Rust `mod`, a C++
//     namespace. These are added, never converted. Resolver indexes explicitly
//     distinguish callable targets from additive declaration labels.
//
// Plus the syntactic facts the edge kinds are derived from later, recorded as
// ordinary property rows with the mechanism in the row itself:
// `implements_type` and `override_marker`.
//
// A new declaration node is NEVER passed down as the walk's parent: doing so
// would relabel the functions inside it as methods and move counts between
// existing labels. Members of a declaration are parented explicitly instead.

import (
	"strings"

	sitter "github.com/smacker/go-tree-sitter"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// Property row kinds this file writes. They are additive: no existing row kind
// changes shape or volume.
const (
	PropSymbolKind     = "symbol_kind"
	PropImplementsType = "implements_type"
	PropOverrideMarker = "override_marker"
)

// maxHeaderBytes bounds the declaration header a keyword refinement reads.
const maxHeaderBytes = 200

// declarationHeader returns the declaration's own leading text: everything up
// to the first `{` or newline, capped. That window holds the modifiers and the
// declaring keyword in every grammar mapped here — `enum class Color {`,
// `public override void F() {`, `impl Trait for T {`, `defmodule Foo do` — and
// stops before a body that could contain the same word in another role.
func declarationHeader(node *sitter.Node, src []byte) string {
	start := int(node.StartByte())
	end := int(node.EndByte())
	if start >= len(src) || start >= end {
		return ""
	}
	if end > len(src) {
		end = len(src)
	}
	if end-start > maxHeaderBytes {
		end = start + maxHeaderBytes
	}
	text := string(src[start:end])
	if i := strings.IndexAny(text, "{\n"); i >= 0 {
		text = text[:i]
	}
	return text
}

// hasDescendantType reports whether node nests a descendant of the given type
// within depth levels. Bounded so a large body cannot make this quadratic.
func hasDescendantType(node *sitter.Node, nodeType string, depth int) bool {
	if node == nil || depth < 0 {
		return false
	}
	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child == nil {
			continue
		}
		if child.Type() == nodeType {
			return true
		}
		if hasDescendantType(child, nodeType, depth-1) {
			return true
		}
	}
	return false
}

// findDescendantType returns the first descendant of the given type within
// depth levels, or nil.
func findDescendantType(node *sitter.Node, nodeType string, depth int) *sitter.Node {
	if node == nil || depth < 0 {
		return nil
	}
	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child == nil {
			continue
		}
		if child.Type() == nodeType {
			return child
		}
		if found := findDescendantType(child, nodeType, depth-1); found != nil {
			return found
		}
	}
	return nil
}

// typeNameSuffix reduces a possibly qualified type reference to the declared
// name a symbol node would carry: `foo::Bar<T>` -> `Bar`, `a.b.C` -> `C`.
func typeNameSuffix(raw string) string {
	name := strings.TrimSpace(raw)
	if i := strings.IndexAny(name, "<("); i > 0 {
		name = name[:i]
	}
	for _, sep := range []string{"::", ".", "\\"} {
		if i := strings.LastIndex(name, sep); i >= 0 {
			name = name[i+len(sep):]
		}
	}
	name = strings.TrimSpace(name)
	if name == "" {
		return ""
	}
	for i := 0; i < len(name); i++ {
		c := name[i]
		if !(c == '_' || c == '$' || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) {
			return ""
		}
	}
	return name
}

// maxImplementsNames bounds one clause. A conformance list longer than this is
// not silently truncated to a wrong answer — the rows written are exactly the
// ones read, and the count is visible in the graph.
const maxImplementsNames = 16

// implementsTypeNames reads the named types out of a syntactic conformance
// clause. Only identifier-shaped descendants are read, so a generic argument
// list or a comment contributes nothing.
func implementsTypeNames(clause *sitter.Node, src []byte) []string {
	if clause == nil {
		return nil
	}
	var out []string
	seen := map[string]bool{}
	var walk func(n *sitter.Node, depth int)
	walk = func(n *sitter.Node, depth int) {
		if n == nil || depth < 0 || len(out) >= maxImplementsNames {
			return
		}
		switch n.Type() {
		case "type_identifier", "identifier", "scoped_type_identifier",
			"qualified_name", "name", "generic_type", "user_type", "simple_identifier":
			if name := typeNameSuffix(n.Content(src)); name != "" && !seen[name] {
				seen[name] = true
				out = append(out, name)
				return
			}
		}
		for i := 0; i < int(n.ChildCount()); i++ {
			walk(n.Child(i), depth-1)
		}
	}
	walk(clause, 4)
	return out
}

// annotateSymbolKind writes the `symbol_kind` row for a node the parser has
// just emitted, and the syntactic conformance and override rows that go with
// it. nodeIdx is the index into result.Nodes.
//
// It is a no-op for a language with no mapping and for a node type the mapping
// does not name, so it can never change what the parser already produced.
func annotateSymbolKind(node *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, nodeIdx int) {
	tax := specs.TaxonomyFor(sf.Language)
	if tax == nil || node == nil || nodeIdx < 0 || nodeIdx >= len(result.Nodes) {
		return
	}
	nodeType := node.Type()
	header := declarationHeader(node, src)
	hasChild := func(childType string) bool {
		return hasDescendantType(node, childType, specs.MaxChildKindDepth)
	}
	kind, ok := tax.KindForNodeType(nodeType, header, hasChild)
	if !ok {
		return
	}
	// A callable whose name is this language's constructor spelling is a
	// constructor. This is syntax in the grammars that reserve the name
	// (`constructor`, `__init__`, `__construct`, `initialize`); it is not
	// applied where the spelling is only a convention, because those languages
	// declare no ConstructorNames.
	if kind == specs.KindMethod || kind == specs.KindFunction {
		if tax.IsConstructorName(result.Nodes[nodeIdx].Name) {
			kind = specs.KindConstructor
		}
	}
	line := int(node.StartPoint().Row) + 1
	result.Properties = append(result.Properties, PropertyRef{
		NodeIdx: nodeIdx, Kind: PropSymbolKind, Value: kind,
		Line: line, Confidence: 1.0,
	})
	annotateSyntacticRelations(node, tax, src, result, nodeIdx, nodeType, header, line)
}

// annotateSyntacticRelations records the two facts that later become the
// IMPLEMENTS and OVERRIDES edges. Both are written with the mechanism that
// produced them, so a reader of the row does not have to trust the writer.
func annotateSyntacticRelations(
	node *sitter.Node, tax *specs.Taxonomy, src []byte, result *ParseResult,
	nodeIdx int, nodeType, header string, line int,
) {
	for _, rule := range tax.Implements {
		if rule.NodeType != nodeType {
			continue
		}
		switch {
		case rule.TraitFrom != "":
			// `impl Trait for Type`: the trait is a named field, so the fact is
			// read rather than inferred. Recorded on the impl node itself.
			traitNode := node.ChildByFieldName(rule.TraitFrom)
			if traitNode == nil {
				continue
			}
			if name := typeNameSuffix(traitNode.Content(src)); name != "" {
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx: nodeIdx, Kind: PropImplementsType,
					Value: name + "|" + specs.MechImplTrait, Line: line, Confidence: 1.0,
				})
			}
		default:
			clause := findDescendantType(node, rule.ChildType, 3)
			for _, name := range implementsTypeNames(clause, src) {
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx: nodeIdx, Kind: PropImplementsType,
					Value: name + "|" + specs.MechImplementsClause, Line: line, Confidence: 1.0,
				})
			}
		}
	}
	if tax.HasOverrideMarker(header) {
		result.Properties = append(result.Properties, PropertyRef{
			NodeIdx: nodeIdx, Kind: PropOverrideMarker,
			Value: specs.MechOverrideMarker, Line: line, Confidence: 1.0,
		})
	}
}

// EmitTaxonomyDeclarations controls the second output: NEW nodes for
// declarations the parser has no entry for.
//
// MEASURED on arktype at 04355e8b (458 files), full index before and after:
// emitting TypeScript Enum / EnumMember / TypeAlias / Namespace nodes added
// 1,565 real symbols and moved eleven pre-existing totals, because the new
// names enter the resolver's name index and compete for call targets. The
// CALLS edges did not just grow, they MOVED:
//
//	target label   before   after
//	Method          3,188   2,266
//	Class             328      17
//	Function        2,417   2,380
//	Namespace           0   1,053
//	TypeAlias           0     460
//
// 1,513 calls that had resolved to a callable now resolve to a namespace or a
// type alias, neither of which is callable. The headline "+243 calls resolved"
// hides 922 method targets lost. A resolver rung reached through the import
// index does not filter by label, so a merged TypeScript `namespace X` shadows
// the `class X` it merges with.
//
// The measured resolver regression above is prevented by BuildNameIndex and
// import resolution filtering through resolver.IsCallTargetLabel and
// resolver.IsCodeSymbolLabel respectively. It remains a var so tests can prove
// the emitter's controlled behavior without maintaining a second code path.
var EmitTaxonomyDeclarations = true

// emitTaxonomyDeclaration emits a node for a declaration the parser has no
// entry for, plus its members. It returns true when it emitted something.
//
// The walk is NOT redirected: the caller keeps its own parent index, so no
// function inside a namespace or module becomes a method of it and no
// pre-existing label total moves.
func emitTaxonomyDeclaration(node *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, isTest bool) bool {
	tax := specs.TaxonomyFor(sf.Language)
	if !EmitTaxonomyDeclarations || tax == nil || node == nil || len(tax.Decls) == 0 {
		return false
	}
	decl, ok := tax.Decls[node.Type()]
	if !ok {
		return false
	}
	name := declarationName(node, decl.NameField, src)
	if name == "" {
		return false
	}
	spec := sf.Spec
	idx := len(result.Nodes)
	result.Nodes = append(result.Nodes, store.Node{
		Label:         decl.Label,
		Name:          name,
		QualifiedName: name,
		FilePath:      sf.Path,
		StartLine:     int(node.StartPoint().Row) + 1,
		EndLine:       int(node.EndPoint().Row) + 1,
		IsExported:    spec != nil && spec.IsExported != nil && spec.IsExported(name),
		IsTest:        isTest,
		Language:      sf.Language,
		ByteStart:     uint64(node.StartByte()),
		ByteEnd:       uint64(node.EndByte()),
	})
	result.Properties = append(result.Properties, PropertyRef{
		NodeIdx: idx, Kind: PropSymbolKind, Value: decl.Kind,
		Line: int(node.StartPoint().Row) + 1, Confidence: 1.0,
	})
	annotateSyntacticRelations(node, tax, src, result, idx, node.Type(),
		declarationHeader(node, src), int(node.StartPoint().Row)+1)
	emitTaxonomyMembers(node, sf, src, result, isTest, idx+1)
	return true
}

// emitTaxonomyMembers emits one node per member of a declaration body. It is
// called for both new declarations and declarations the parser already emits,
// so a Java enum gets its constants without the enum node changing at all.
// parentNodeIdx is the 1-based convention the store uses for ParentID.
func emitTaxonomyMembers(
	node *sitter.Node, sf walker.SourceFile, src []byte,
	result *ParseResult, isTest bool, parentNodeIdx int,
) {
	tax := specs.TaxonomyFor(sf.Language)
	if !EmitTaxonomyDeclarations || tax == nil || node == nil || len(tax.MemberDecls) == 0 {
		return
	}
	member, ok := tax.MemberDecls[node.Type()]
	if !ok {
		return
	}
	container := node
	if member.Of != "" {
		container = findDescendantType(node, member.Of, 2)
		if container == nil {
			return
		}
	}
	parentName := ""
	if parentNodeIdx > 0 && parentNodeIdx-1 < len(result.Nodes) {
		parentName = result.Nodes[parentNodeIdx-1].Name
	}
	wanted := map[string]bool{}
	for _, t := range member.Types {
		wanted[t] = true
	}
	for i := 0; i < int(container.ChildCount()); i++ {
		child := container.Child(i)
		if child == nil || !wanted[child.Type()] {
			continue
		}
		name := declarationName(child, member.NameField, src)
		if name == "" {
			continue
		}
		qualified := name
		if parentName != "" {
			qualified = parentName + "." + name
		}
		idx := len(result.Nodes)
		result.Nodes = append(result.Nodes, store.Node{
			Label:         member.Label,
			Name:          name,
			QualifiedName: qualified,
			FilePath:      sf.Path,
			StartLine:     int(child.StartPoint().Row) + 1,
			EndLine:       int(child.EndPoint().Row) + 1,
			IsTest:        isTest,
			Language:      sf.Language,
			ParentID:      int64(parentNodeIdx),
			ByteStart:     uint64(child.StartByte()),
			ByteEnd:       uint64(child.EndByte()),
		})
		result.Properties = append(result.Properties, PropertyRef{
			NodeIdx: idx, Kind: PropSymbolKind, Value: member.Kind,
			Line: int(child.StartPoint().Row) + 1, Confidence: 1.0,
		})
	}
}

// declarationName reads a declaration's name from the named field, falling
// back to the first identifier-shaped descendant, then to the node's own text.
// A name that is not identifier-shaped is refused rather than guessed at.
func declarationName(node *sitter.Node, field string, src []byte) string {
	if node == nil {
		return ""
	}
	if field != "" {
		if named := node.ChildByFieldName(field); named != nil {
			if name := typeNameSuffix(named.Content(src)); name != "" {
				return name
			}
		}
	}
	if id := extractFirstIdentifier(node, src); id != "" {
		if name := typeNameSuffix(id); name != "" {
			return name
		}
	}
	return typeNameSuffix(node.Content(src))
}
