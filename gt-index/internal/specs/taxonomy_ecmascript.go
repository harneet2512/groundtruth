package specs

// JavaScript / TypeScript / Svelte.
//
// NOTE ON THE FILENAME: this file must not be called taxonomy_js.go. Go treats
// a trailing `_js` as the GOOS build constraint for WebAssembly, so such a file
// is excluded from every ordinary build and its init() never runs.
//
// TypeScript is the one grammar in this repository that separates an
// `implements_clause` from `extends`, so it is one of the few languages where
// IMPLEMENTS is a syntactic fact rather than a guess about a base-class name.
//
// `abstract_class_declaration` remains absent because the current parser does
// not emit it as a class declaration; the taxonomy never invents an unreachable
// declaration.

func init() {
	RegisterTaxonomy(&Taxonomy{
		Lang: "javascript",
		ByNodeType: map[string]string{
			"function_declaration":           KindFunction,
			"generator_function_declaration": KindFunction,
			"arrow_function":                 KindFunction,
			"method_definition":              KindMethod,
			"class_declaration":              KindClass,
			"class":                          KindClass,
		},
		Keywords: []KeywordKind{
			{NodeType: "method_definition", Keyword: "get", Kind: KindAccessor},
			{NodeType: "method_definition", Keyword: "set", Kind: KindAccessor},
		},
		ConstructorNames: []string{"constructor"},
		OverrideMarkers:  nil, // `override` is a TypeScript keyword, not a JS one
		// GT's parser mints a node per `it(...)` / `test(...)` block.
		SynthesizedKinds: []string{KindTest},
		AsProperty: map[string]string{
			KindDecorator: "class_decorator",
			KindField:     "class_field",
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindTrait, KindImpl, KindRecord, KindUnion, KindMacro,
				KindAnnotation, KindProtocol, KindConstant,
			),
			map[string]string{
				KindInterface:  "JavaScript has no interface declaration; the construct is TypeScript-only",
				KindStruct:     "JavaScript has no struct declaration",
				KindEnum:       "JavaScript has no enum declaration; the construct is TypeScript-only",
				KindEnumMember: "no enum declaration, so no member declaration",
				KindTypeAlias:  "JavaScript carries no type syntax",
				KindNamespace:  "ES modules replaced the namespace construct; the grammar has no namespace declaration",
				KindModule:     "an ES module is a file; GT already emits the File node",
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang: "typescript",
		ByNodeType: map[string]string{
			"function_declaration":           KindFunction,
			"generator_function_declaration": KindFunction,
			"arrow_function":                 KindFunction,
			"method_definition":              KindMethod,
			"class_declaration":              KindClass,
			"class":                          KindClass,
			"interface_declaration":          KindInterface,
		},
		Keywords: []KeywordKind{
			{NodeType: "method_definition", Keyword: "get", Kind: KindAccessor},
			{NodeType: "method_definition", Keyword: "set", Kind: KindAccessor},
		},
		ConstructorNames: []string{"constructor"},
		Decls: map[string]Decl{
			"enum_declaration":       {Kind: KindEnum, Label: "Enum", NameField: "name"},
			"type_alias_declaration": {Kind: KindTypeAlias, Label: "TypeAlias", NameField: "name"},
			"internal_module":        {Kind: KindNamespace, Label: "Namespace", NameField: "name"},
			"module":                 {Kind: KindNamespace, Label: "Namespace", NameField: "name"},
		},
		MemberDecls: map[string]MemberDecl{
			"enum_declaration": {
				Kind: KindEnumMember, Label: "EnumMember", Of: "enum_body",
				Types: []string{"property_identifier", "enum_assignment"}, NameField: "name",
			},
		},
		Implements: []ImplementsRule{
			{NodeType: "class_declaration", ChildType: "implements_clause"},
			{NodeType: "class", ChildType: "implements_clause"},
		},
		OverrideMarkers: []string{"override"},
		// GT's parser mints a node per `it(...)` / `test(...)` block.
		SynthesizedKinds: []string{KindTest},
		AsProperty: map[string]string{
			KindDecorator: "class_decorator",
			KindField:     "class_field",
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindTrait, KindImpl, KindRecord, KindMacro, KindAnnotation,
			),
			map[string]string{
				KindStruct:   "TypeScript has no struct declaration; an object type is a type_alias_declaration",
				KindProtocol: "TypeScript spells structural conformance as an interface_declaration",
				KindUnion:    "a union is a type EXPRESSION inside a type_alias_declaration, not a declaration of its own",
				KindModule:   "an ES module is a file; GT already emits the File node. `module X {}` is mapped to namespace",
				KindConstant: "`const` binds a variable; the grammar does not distinguish a constant declaration",
			},
		),
	})

	// Svelte's pinned grammar exposes template elements, while script and style
	// bodies are opaque raw_text. Emit the reachable template declaration and do
	// not claim JavaScript or CSS symbols that this grammar cannot parse.
	RegisterTaxonomy(&Taxonomy{
		Lang: "svelte",
		Decls: map[string]Decl{
			"element": {Kind: KindElement, Label: "Element", NameField: ""},
		},
		Absent: mergeReasons(
			absent(ReasonNoSynthTest, KindTest),
			noCodeKinds(),
			absent(ReasonNotCode, KindTable, KindMessage, KindService, KindResource),
			map[string]string{
				KindRule:    "style_element holds CSS as raw_text; this grammar does not parse it",
				KindSection: ReasonMarkupOnly,
			},
		),
	})
}
