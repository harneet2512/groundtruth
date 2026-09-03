package specs

// C#.
//
// The grammar names records, enums, namespaces and enum members, none of
// which the parser emits today, so all four arrive as new labels.
//
// IMPLEMENTS is NOT available: `base_list` holds the base class and every
// implemented interface in one comma-separated list with identical syntax.
// Deciding which entry is an interface needs the declaration of the named
// type, which is a resolution question, not a syntactic one. The `I` prefix is
// a convention and is not used here.
func init() {
	RegisterTaxonomy(&Taxonomy{
		Lang: "csharp",
		ByNodeType: map[string]string{
			"method_declaration":      KindMethod,
			"constructor_declaration": KindConstructor,
			"class_declaration":       KindClass,
			"interface_declaration":   KindInterface,
			"struct_declaration":      KindStruct,
		},
		Decls: map[string]Decl{
			"enum_declaration":                  {Kind: KindEnum, Label: "Enum", NameField: "name"},
			"record_declaration":                {Kind: KindRecord, Label: "Record", NameField: "name"},
			"namespace_declaration":             {Kind: KindNamespace, Label: "Namespace", NameField: "name"},
			"file_scoped_namespace_declaration": {Kind: KindNamespace, Label: "Namespace", NameField: "name"},
		},
		MemberDecls: map[string]MemberDecl{
			"enum_declaration": {
				Kind: KindEnumMember, Label: "EnumMember", Of: "enum_member_declaration_list",
				Types: []string{"enum_member_declaration"}, NameField: "name",
			},
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
			absent(ReasonNoConstruct, KindTrait, KindImpl, KindUnion, KindMacro),
			map[string]string{
				KindFunction:   "C# has no free function; every callable is a method_declaration",
				KindAccessor:   "accessor_declaration exists but carries no name of its own, and the property_declaration it belongs to is not in the parser's declaration set (see REPORT.md gaps)",
				KindProtocol:   "C# spells the protocol construct `interface`",
				KindModule:     "a C# module is an assembly, not a source declaration",
				KindAnnotation: "an attribute type is an ordinary class_declaration deriving from Attribute; the declaration node is identical",
				KindConstant:   "`const` is a modifier on a field_declaration, not a constant declaration form",
				KindTypeAlias:  "`using X = Y;` is a using_directive scoped to the file, not a declaration of a named type",
			},
		),
	})
}
