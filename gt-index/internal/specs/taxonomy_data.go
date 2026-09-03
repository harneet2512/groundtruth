package specs

// Data, schema and markup grammars: Protocol Buffers, SQL, CSS, HTML,
// Markdown, TOML, YAML, HCL, CUE.
//
// These grammars declare things, but not the things a code taxonomy is made
// of. Rather than leave nine of the thirty languages blank — which would read
// as "not looked at" — each states which of the code kinds it lacks and why,
// and claims only the data kinds it actually has.
//
// MEASURED (2026-09-03, tree-sitter symbol table of the pinned grammar): the
// SQL grammar has no `create_table_statement` or `create_function_statement`
// node type, which are the two names GT's sql spec looks for, so GT emits no
// SQL symbol at all. The mapping names the grammar's real node types; the
// parser will not reach them until the sql spec is corrected, which is out of
// this item's scope. See REPORT.md.

func init() {
	RegisterTaxonomy(&Taxonomy{
		Lang: "protobuf",
		ByNodeType: map[string]string{
			"message": KindMessage,
			"service": KindService,
			"enum":    KindEnum,
			"rpc":     KindMethod,
		},
		MemberDecls: map[string]MemberDecl{
			"enum": {
				Kind: KindEnumMember, Label: "EnumMember", Of: "enum_body",
				Types: []string{"enum_field"}, NameField: "",
			},
		},
		Absent: mergeReasons(
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindFunction, KindConstructor, KindAccessor, KindClass,
				KindInterface, KindStruct, KindTrait, KindImpl, KindProtocol,
				KindTypeAlias, KindRecord, KindUnion, KindNamespace, KindMacro,
				KindAnnotation, KindDecorator, KindConstant,
			),
			map[string]string{
				KindModule:   "a .proto file is the compilation unit; `package` declares no named symbol node",
				KindField:    "message fields are `field` nodes; GT records no class_field property for protobuf (see REPORT.md gaps)",
				KindTable:    ReasonNotCode,
				KindResource: ReasonNotCode,
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang: "sql",
		ByNodeType: map[string]string{
			"create_table":    KindTable,
			"create_function": KindFunction,
		},
		Absent: mergeReasons(
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindMethod, KindConstructor, KindAccessor, KindClass,
				KindInterface, KindStruct, KindTrait, KindImpl, KindProtocol,
				KindRecord, KindUnion, KindNamespace, KindModule, KindMacro,
				KindAnnotation, KindDecorator, KindConstant,
			),
			map[string]string{
				KindEnum:       "create_type with an enum_elements list is an enum, but GT's sql spec reaches no SQL declaration at all (see REPORT.md)",
				KindEnumMember: "no enum declaration is reached, so no member declaration",
				KindTypeAlias:  "create_type declares a new type rather than aliasing one",
				KindField:      "a column_definition is a field, but GT records no class_field property for SQL",
				KindMessage:    ReasonNotCode,
				KindService:    ReasonNotCode,
				KindResource:   ReasonNotCode,
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang:       "css",
		ByNodeType: map[string]string{"rule_set": KindRule},
		Absent: mergeReasons(
			noCodeKinds(),
			absent(ReasonNotCode, KindTable, KindMessage, KindService, KindResource),
			map[string]string{
				KindElement: "a CSS selector names elements it does not declare",
				KindSection: ReasonMarkupOnly,
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang:       "html",
		ByNodeType: map[string]string{"element": KindElement},
		Absent: mergeReasons(
			noCodeKinds(),
			absent(ReasonNotCode, KindTable, KindMessage, KindService, KindResource),
			map[string]string{
				KindRule:    "style rules live in a style_element's raw text, which this grammar does not parse as CSS",
				KindSection: "HTML sectioning is an element name, already covered by element",
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang:       "markdown",
		ByNodeType: map[string]string{"section": KindSection},
		Absent: mergeReasons(
			noCodeKinds(),
			absent(ReasonNotCode, KindMessage, KindService, KindResource),
			map[string]string{
				KindTable:   "pipe_table is a rendering of tabular text, not a table declaration",
				KindRule:    ReasonMarkupOnly,
				KindElement: "Markdown has no element declaration; html_block is opaque text",
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang:       "toml",
		ByNodeType: map[string]string{"table": KindResource},
		Absent: mergeReasons(
			noCodeKinds(),
			noMarkupKinds(),
			absent(ReasonNotCode, KindMessage, KindService),
			map[string]string{
				KindTable: "a TOML `table` is a configuration section, mapped to resource; it declares no relation",
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang:       "yaml",
		ByNodeType: map[string]string{"block_mapping": KindResource},
		Absent: mergeReasons(
			noCodeKinds(),
			noMarkupKinds(),
			absent(ReasonNotCode, KindTable, KindMessage, KindService),
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang:       "hcl",
		ByNodeType: map[string]string{"block": KindResource},
		Absent: mergeReasons(
			noCodeKinds(),
			noMarkupKinds(),
			absent(ReasonNotCode, KindTable, KindMessage, KindService),
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang: "cue",
		// GT's cue spec declares no function or class node types, so the parser
		// emits no CUE symbol; the grammar's `field` and `struct_lit` are
		// values, not declarations of a named symbol GT indexes.
		ByNodeType: nil,
		Absent: mergeReasons(
			noCodeKinds(),
			noMarkupKinds(),
			absent(ReasonNotCode, KindTable, KindMessage, KindService),
			map[string]string{
				KindResource: "a CUE struct_lit is a value, and GT's cue spec declares no declaration node types to walk",
			},
		),
	})
}
