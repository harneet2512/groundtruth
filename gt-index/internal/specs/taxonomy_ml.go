package specs

// ML-family: OCaml and Elm.
//
// Both are type-first languages whose primary declaration is a type
// definition, which is exactly the axis GT was blind on: today every OCaml
// `type_definition` and every Elm `type_declaration` lands under the single
// `Class` label.

func init() {
	RegisterTaxonomy(&Taxonomy{
		Lang: "ocaml",
		ByNodeType: map[string]string{
			"value_definition":  KindFunction,
			"let_binding":       KindFunction,
			"type_definition":   KindTypeAlias,
			"module_definition": KindModule,
		},
		ChildKinds: []ChildKind{
			// A type definition that lists variants is a sum type, not an
			// alias; the grammar says so with a nested variant_declaration.
			{NodeType: "type_definition", ChildType: "variant_declaration", Kind: KindUnion},
			{NodeType: "type_definition", ChildType: "record_declaration", Kind: KindRecord},
		},
		Implements:      nil,
		OverrideMarkers: nil,
		Absent: mergeReasons(
			absent(ReasonNoSynthTest, KindTest),
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindStruct, KindEnum, KindTrait, KindImpl, KindProtocol,
				KindMacro, KindAnnotation, KindDecorator,
			),
			map[string]string{
				KindMethod:      "method_definition exists in the grammar but OCaml's object system is not in the parser's declaration set (see REPORT.md gaps)",
				KindConstructor: "constructor_declaration names a variant constructor, not an object constructor",
				KindAccessor:    ReasonNoAccessor,
				KindClass:       "class_definition exists in the grammar but is not in the parser's declaration set (see REPORT.md gaps)",
				KindInterface:   "OCaml spells the interface construct `module_type_definition`, which is not in the parser's declaration set (see REPORT.md gaps)",
				KindEnumMember:  "a variant is inside variant_declaration; emitting variants is out of this item's scope",
				KindNamespace:   "OCaml spells the namespace construct `module`, already mapped",
				KindField:       "field_declaration lives inside record_declaration; GT records no class_field property for OCaml",
				KindConstant:    "a constant is a value_definition binding a literal, indistinguishable from any other binding",
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang: "elm",
		ByNodeType: map[string]string{
			"value_declaration":      KindFunction,
			"type_declaration":       KindUnion,
			"type_alias_declaration": KindTypeAlias,
		},
		MemberDecls: map[string]MemberDecl{
			// `type Msg = Inc | Dec` — each union_variant is a named member of
			// the declaration, which is the closest Elm has to an enum member.
			"type_declaration": {
				Kind: KindEnumMember, Label: "EnumMember", Of: "",
				Types: []string{"union_variant"}, NameField: "name",
			},
		},
		Implements:      nil,
		OverrideMarkers: nil,
		Absent: mergeReasons(
			absent(ReasonNoSynthTest, KindTest),
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindMethod, KindConstructor, KindAccessor, KindClass, KindInterface,
				KindStruct, KindTrait, KindImpl, KindProtocol, KindMacro,
				KindAnnotation, KindDecorator,
			),
			map[string]string{
				KindEnum:      "Elm has no enum; a closed set of tags is a type_declaration, already mapped to union",
				KindRecord:    "an Elm record is a type EXPRESSION inside a type_alias_declaration, not a declaration of its own",
				KindNamespace: "Elm spells the namespace construct `module`, one per file; GT already emits the File node",
				KindModule:    "module_declaration names the file's own module and declares no additional symbol",
				KindField:     "record fields are part of a type expression, not declarations",
				KindConstant:  "a constant is a value_declaration binding a literal, indistinguishable from any other binding",
			},
		),
	})
}
