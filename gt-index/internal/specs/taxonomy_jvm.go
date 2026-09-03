package specs

// JVM family: Java, Kotlin, Groovy, Scala.
//
// Java is the reference case for both new edge kinds this family supports:
// `super_interfaces` is a dedicated clause, so IMPLEMENTS is read rather than
// inferred, and `@Override` is a marker the compiler itself checks, so
// OVERRIDES has a syntactic witness.
//
// Kotlin, Groovy and Scala all fold interface conformance into one clause that
// also carries the superclass (`delegation_specifier`, `superclass`,
// `extends … with …`). That clause does not say WHICH of its entries is an
// interface, so those three declare IMPLEMENTS unavailable instead of
// guessing from a name.

func init() {
	RegisterTaxonomy(&Taxonomy{
		Lang: "java",
		ByNodeType: map[string]string{
			"method_declaration":      KindMethod,
			"constructor_declaration": KindConstructor,
			"class_declaration":       KindClass,
			"interface_declaration":   KindInterface,
			"enum_declaration":        KindEnum,
		},
		Decls: map[string]Decl{
			"record_declaration":          {Kind: KindRecord, Label: "Record", NameField: "name"},
			"annotation_type_declaration": {Kind: KindAnnotation, Label: "Annotation", NameField: "name"},
		},
		MemberDecls: map[string]MemberDecl{
			"enum_declaration": {
				Kind: KindEnumMember, Label: "EnumMember", Of: "enum_body",
				Types: []string{"enum_constant"}, NameField: "name",
			},
		},
		Implements: []ImplementsRule{
			{NodeType: "class_declaration", ChildType: "super_interfaces"},
			{NodeType: "enum_declaration", ChildType: "super_interfaces"},
			{NodeType: "record_declaration", ChildType: "super_interfaces"},
		},
		OverrideMarkers: []string{"@Override"},
		AsProperty: map[string]string{
			KindDecorator: "class_decorator",
			KindField:     "class_field",
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct, KindTrait, KindImpl, KindUnion, KindMacro),
			map[string]string{
				KindFunction:  "Java has no free function; every callable is a method_declaration",
				KindAccessor:  "getX/setX is a JavaBeans naming convention, not a declaration form the grammar distinguishes",
				KindStruct:    "Java has no struct; the nearest declaration is record_declaration",
				KindProtocol:  "Java spells the protocol construct `interface`",
				KindNamespace: "Java spells the namespace construct `package`, which declares no named symbol node",
				KindModule:    "module-info.java is a separate compilation unit form, not a declaration inside an ordinary source file",
				KindConstant:  "`static final` is a modifier on a field_declaration, not a constant declaration form",
				KindTypeAlias: ReasonNoTypeSyntax,
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang: "kotlin",
		ByNodeType: map[string]string{
			"function_declaration": KindFunction,
			"class_declaration":    KindClass,
			"object_declaration":   KindClass,
		},
		Keywords: []KeywordKind{
			{NodeType: "class_declaration", Keyword: "interface", Kind: KindInterface},
			{NodeType: "class_declaration", Keyword: "enum", Kind: KindEnum},
			{NodeType: "class_declaration", Keyword: "data", Kind: KindRecord},
		},
		Decls: map[string]Decl{
			"type_alias": {Kind: KindTypeAlias, Label: "TypeAlias", NameField: "type_identifier"},
		},
		Implements:      nil,
		OverrideMarkers: []string{"override"},
		AsProperty: map[string]string{
			KindDecorator: "class_decorator",
			KindField:     "class_field",
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct, KindTrait, KindImpl, KindStruct, KindUnion, KindMacro),
			map[string]string{
				KindMethod:      "a Kotlin method is a function_declaration nested in a class body; the declaration node is identical to a top-level function",
				KindConstructor: "primary_constructor and secondary_constructor are not in the parser's declaration set (see REPORT.md gaps)",
				KindAccessor:    "`get()`/`set()` are property accessors inside property_declaration, which the parser does not emit (see REPORT.md gaps)",
				KindEnumMember:  "enum entries live in enum_class_body, which the parser does not descend for members (see REPORT.md gaps)",
				KindProtocol:    "Kotlin spells the protocol construct `interface`",
				KindNamespace:   "Kotlin spells the namespace construct `package`, which declares no named symbol node",
				KindModule:      "a Kotlin module is a build-system unit, not a source declaration",
				KindAnnotation:  "an annotation class is a class_declaration with a modifier; the declaration node is identical to any other class",
				KindConstant:    "`const val` is a modifier on a property, not a constant declaration form",
			},
		),
	})

	// Groovy's pinned grammar is Java-shaped: it names interface, enum, record
	// and annotation-type declarations, and a dedicated `super_interfaces`
	// clause. GT's groovy spec lists only class_declaration and
	// method_declaration, so the other four declarations produce no node today
	// and arrive here as new labels.
	RegisterTaxonomy(&Taxonomy{
		Lang: "groovy",
		ByNodeType: map[string]string{
			"method_declaration": KindMethod,
			"class_declaration":  KindClass,
		},
		Decls: map[string]Decl{
			"interface_declaration":       {Kind: KindInterface, Label: "Interface", NameField: "name"},
			"enum_declaration":            {Kind: KindEnum, Label: "Enum", NameField: "name"},
			"record_declaration":          {Kind: KindRecord, Label: "Record", NameField: "name"},
			"annotation_type_declaration": {Kind: KindAnnotation, Label: "Annotation", NameField: "name"},
			"constructor_declaration":     {Kind: KindConstructor, Label: "Constructor", NameField: "name"},
		},
		MemberDecls: map[string]MemberDecl{
			"enum_declaration": {
				Kind: KindEnumMember, Label: "EnumMember", Of: "enum_body",
				Types: []string{"enum_constant"}, NameField: "name",
			},
		},
		Implements: []ImplementsRule{
			{NodeType: "class_declaration", ChildType: "super_interfaces"},
			{NodeType: "enum_declaration", ChildType: "super_interfaces"},
			{NodeType: "record_declaration", ChildType: "super_interfaces"},
		},
		OverrideMarkers: []string{"@Override"},
		AsProperty: map[string]string{
			KindDecorator: "class_decorator",
			KindField:     "class_field",
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct, KindImpl, KindStruct, KindUnion, KindMacro, KindTrait),
			map[string]string{
				KindFunction:  "a Groovy script body is not a named declaration; every named callable is a method_declaration",
				KindAccessor:  "getX/setX is a convention, not a declaration form",
				KindProtocol:  "Groovy spells the protocol construct `interface`",
				KindTypeAlias: ReasonNoTypeSyntax,
				KindNamespace: "Groovy spells the namespace construct `package`, which declares no named symbol node",
				KindModule:    "module_declaration describes a JPMS module, not a declaration inside an ordinary source file",
				KindConstant:  "`final` is a modifier, not a constant declaration form",
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang: "scala",
		ByNodeType: map[string]string{
			"function_definition": KindFunction,
			"val_definition":      KindConstant,
			"class_definition":    KindClass,
			"object_definition":   KindClass,
			"trait_definition":    KindTrait,
		},
		Keywords: []KeywordKind{
			{NodeType: "class_definition", Keyword: "case", Kind: KindRecord},
		},
		Decls: map[string]Decl{
			"type_definition": {Kind: KindTypeAlias, Label: "TypeAlias", NameField: "name"},
			"enum_definition": {Kind: KindEnum, Label: "Enum", NameField: "name"},
		},
		Implements:      nil,
		OverrideMarkers: []string{"override"},
		AsProperty: map[string]string{
			KindField: "class_field",
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct, KindImpl, KindStruct, KindUnion, KindMacro, KindAnnotation, KindDecorator),
			map[string]string{
				KindMethod:      "a Scala method is a function_definition inside a template body; the declaration node is identical to a top-level def",
				KindConstructor: "the primary constructor is the class parameter list, not a declaration of its own",
				KindAccessor:    "a Scala field IS its accessor; there is no separate declaration form",
				KindInterface:   "Scala spells the interface construct `trait`",
				KindProtocol:    "Scala spells the protocol construct `trait`",
				KindEnumMember:  "Scala 3 enum cases are not exposed as a distinct member declaration in this grammar's parser mapping",
				KindNamespace:   "Scala spells the namespace construct `package`, which declares no named symbol node",
				KindModule:      ReasonNoModuleDecl,
			},
		),
	})
}
