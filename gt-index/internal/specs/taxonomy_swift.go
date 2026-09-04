package specs

// Swift.
//
// MEASURED (2026-09-03, tree-sitter symbol table of the pinned grammar): the
// Swift grammar has NO `struct_declaration` node type, which is one of the
// three names GT's swift spec lists in ClassNodes. Struct, enum, actor and
// extension declarations are all `class_declaration` nodes distinguished by
// their leading keyword, so a Swift struct is currently indistinguishable from
// a Swift class in GT. That is precisely what the keyword refinement below
// fixes, and it fixes it without moving the `Class` label total: the node is
// already emitted, only its kind changes.
//
// IMPLEMENTS is not available: `inheritance_specifier` carries a superclass and
// a protocol conformance with identical syntax.
func init() {
	RegisterTaxonomy(&Taxonomy{
		Lang: "swift",
		ByNodeType: map[string]string{
			"function_declaration": KindFunction,
			"class_declaration":    KindClass,
			"protocol_declaration": KindProtocol,
		},
		Keywords: []KeywordKind{
			{NodeType: "class_declaration", Keyword: "struct", Kind: KindStruct},
			{NodeType: "class_declaration", Keyword: "enum", Kind: KindEnum},
			{NodeType: "class_declaration", Keyword: "extension", Kind: KindImpl},
		},
		Decls: map[string]Decl{
			"typealias_declaration": {Kind: KindTypeAlias, Label: "TypeAlias", NameField: "name"},
			"macro_declaration":     {Kind: KindMacro, Label: "Macro", NameField: "name"},
			"init_declaration":      {Kind: KindConstructor, Label: "Constructor", NameField: ""},
		},
		MemberDecls: map[string]MemberDecl{
			"class_declaration": {
				Kind: KindEnumMember, Label: "EnumMember", Of: "enum_class_body",
				Types: []string{"enum_entry"}, NameField: "name",
			},
		},
		Implements:      nil,
		OverrideMarkers: []string{"override"},
		AsProperty: map[string]string{
			KindField: "class_field",
		},
		Absent: mergeReasons(
			absent(ReasonNoSynthTest, KindTest),
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct, KindTrait, KindUnion, KindRecord, KindAnnotation),
			map[string]string{
				KindMethod:    "a Swift method is a function_declaration inside a class body; the declaration node is identical to a free function",
				KindAccessor:  "computed_property carries getter_specifier/setter_specifier, but property_declaration is not in the parser's declaration set (see REPORT.md gaps)",
				KindInterface: "Swift spells the interface construct `protocol`, already mapped",
				KindNamespace: "Swift has no namespace declaration; an enum with static members is a convention",
				KindModule:    "a Swift module is a build target, not a source declaration",
				KindDecorator: "property wrappers and attributes decorate declarations, but GT records no decorator property for Swift (see REPORT.md gaps)",
				KindConstant:  "`let` binds a value; the grammar does not distinguish a constant declaration",
			},
		),
	})
}
