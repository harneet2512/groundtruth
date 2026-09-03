package specs

// C and C++.
//
// Both grammars name enum, union and typedef declarations, none of which the
// parser emits today, so all three arrive as new labels and no pre-existing
// total moves. C++ adds namespaces and `using X = Y` aliases.
//
// Neither language gets IMPLEMENTS: `base_class_clause` carries base classes
// and abstract base classes in the same list with the same syntax, so the
// grammar does not state which entry is an interface.

func init() {
	RegisterTaxonomy(&Taxonomy{
		Lang: "c",
		ByNodeType: map[string]string{
			"function_definition": KindFunction,
			"struct_specifier":    KindStruct,
		},
		Decls: map[string]Decl{
			"enum_specifier":  {Kind: KindEnum, Label: "Enum", NameField: "name"},
			"union_specifier": {Kind: KindUnion, Label: "Union", NameField: "name"},
			"type_definition": {Kind: KindTypeAlias, Label: "TypeAlias", NameField: "declarator"},
		},
		MemberDecls: map[string]MemberDecl{
			"enum_specifier": {
				Kind: KindEnumMember, Label: "EnumMember", Of: "enumerator_list",
				Types: []string{"enumerator"}, NameField: "name",
			},
		},
		AsProperty: map[string]string{
			KindField: "class_field",
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindClass, KindInterface, KindTrait, KindImpl, KindProtocol,
				KindRecord, KindNamespace, KindModule, KindAnnotation, KindDecorator,
			),
			map[string]string{
				KindMethod:      "C has no method; every callable is a free function_definition",
				KindConstructor: ReasonNoConstructor,
				KindAccessor:    ReasonNoAccessor,
				KindMacro:       "preproc_def is a preprocessor directive, not a declaration the parser walks (see REPORT.md gaps)",
				KindConstant:    "`const` is a type qualifier on a declaration, not a constant declaration form",
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang: "cpp",
		ByNodeType: map[string]string{
			"function_definition": KindFunction,
			"class_specifier":     KindClass,
			"struct_specifier":    KindStruct,
		},
		Decls: map[string]Decl{
			"enum_specifier":       {Kind: KindEnum, Label: "Enum", NameField: "name"},
			"union_specifier":      {Kind: KindUnion, Label: "Union", NameField: "name"},
			"type_definition":      {Kind: KindTypeAlias, Label: "TypeAlias", NameField: "declarator"},
			"alias_declaration":    {Kind: KindTypeAlias, Label: "TypeAlias", NameField: "name"},
			"namespace_definition": {Kind: KindNamespace, Label: "Namespace", NameField: "name"},
		},
		MemberDecls: map[string]MemberDecl{
			"enum_specifier": {
				Kind: KindEnumMember, Label: "EnumMember", Of: "enumerator_list",
				Types: []string{"enumerator"}, NameField: "name",
			},
		},
		// `override` is a virt-specifier that follows the parameter list, so it
		// is inside the header window (up to the first `{` or newline).
		OverrideMarkers: []string{"override"},
		AsProperty: map[string]string{
			KindField: "class_field",
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindTrait, KindImpl, KindProtocol, KindRecord, KindModule,
				KindAnnotation, KindDecorator,
			),
			map[string]string{
				KindMethod:      "a C++ member function is a function_definition inside a class_specifier; the declaration node is identical to a free function",
				KindConstructor: "a constructor is a function_definition whose declarator name equals the class; that equality is not a syntactic marker",
				KindAccessor:    ReasonNoAccessor,
				KindInterface:   "C++ has no interface declaration; an abstract base class is an ordinary class_specifier",
				KindMacro:       "preproc_def is a preprocessor directive, not a declaration the parser walks (see REPORT.md gaps)",
				KindConstant:    "`const`/`constexpr` are qualifiers on a declaration, not a constant declaration form",
			},
		),
	})
}
