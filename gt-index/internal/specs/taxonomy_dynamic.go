package specs

// Dynamic languages: PHP, Ruby, Lua, Bash, Elixir.
//
// PHP is the third grammar here with a dedicated `class_interface_clause`, so
// it gets IMPLEMENTS alongside Java and TypeScript.
//
// Elixir has no declaration node types at all: every definition is a `call`
// whose first token names the form. The kind is therefore read from the
// declaration header — `defmodule`, `defprotocol`, `defimpl`, `defmacro`,
// `defstruct` — which is the grammar's only statement of what the call is.
//
// MEASURED (2026-09-03, tree-sitter symbol tables of the pinned grammars):
// the Lua grammar has no `function_declaration` or `function_definition_statement`
// node type, which are the two names GT's lua spec looks for, so GT emits no Lua
// symbol at all. The mapping below names the grammar's real node types so the
// taxonomy is truthful about the language; the parser will not reach them until
// the lua spec is corrected, which is out of this item's scope. See REPORT.md.

func init() {
	RegisterTaxonomy(&Taxonomy{
		Lang: "php",
		ByNodeType: map[string]string{
			"function_definition":   KindFunction,
			"method_declaration":    KindMethod,
			"class_declaration":     KindClass,
			"interface_declaration": KindInterface,
		},
		Decls: map[string]Decl{
			"trait_declaration":    {Kind: KindTrait, Label: "Trait", NameField: "name"},
			"enum_declaration":     {Kind: KindEnum, Label: "Enum", NameField: "name"},
			"namespace_definition": {Kind: KindNamespace, Label: "Namespace", NameField: "name"},
		},
		MemberDecls: map[string]MemberDecl{
			"enum_declaration": {
				Kind: KindEnumMember, Label: "EnumMember", Of: "enum_declaration_list",
				Types: []string{"enum_case"}, NameField: "name",
			},
		},
		ConstructorNames: []string{"__construct"},
		Implements: []ImplementsRule{
			{NodeType: "class_declaration", ChildType: "class_interface_clause"},
			{NodeType: "enum_declaration", ChildType: "class_interface_clause"},
		},
		OverrideMarkers: nil,
		AsProperty: map[string]string{
			KindDecorator: "class_decorator",
			KindField:     "class_field",
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct, KindImpl, KindStruct, KindUnion, KindMacro, KindRecord, KindAnnotation),
			map[string]string{
				KindAccessor:  "PHP has no accessor declaration; __get/__set are magic methods, which is a naming convention",
				KindProtocol:  "PHP spells the protocol construct `interface`",
				KindTypeAlias: ReasonNoTypeSyntax,
				KindModule:    "a PHP file is the compilation unit; GT already emits the File node",
				KindConstant:  "const_declaration is not in the parser's declaration set (see REPORT.md gaps)",
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang: "ruby",
		ByNodeType: map[string]string{
			"method":           KindMethod,
			"singleton_method": KindMethod,
			"class":            KindClass,
			"module":           KindModule,
		},
		ConstructorNames: []string{"initialize"},
		Implements:       nil,
		OverrideMarkers:  nil,
		AsProperty: map[string]string{
			KindField: "class_field",
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindStruct, KindInterface, KindTrait, KindImpl, KindProtocol,
				KindRecord, KindUnion, KindAnnotation, KindDecorator,
			),
			map[string]string{
				KindFunction:   "a top-level Ruby def is still a `method` node; the grammar has no separate function form",
				KindAccessor:   "attr_accessor is a method call, not a declaration",
				KindEnum:       "Ruby has no enum declaration",
				KindEnumMember: "no enum declaration, so no member declaration",
				KindTypeAlias:  ReasonNoTypeSyntax,
				KindNamespace:  "Ruby spells the namespace construct `module`, already mapped",
				KindMacro:      "Ruby metaprogramming is ordinary method calls, not a macro declaration",
				KindConstant:   "a Ruby constant is an assignment to a capitalised identifier, not a declaration form",
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang: "lua",
		ByNodeType: map[string]string{
			// The grammar's real names. GT's lua spec looks for other names and
			// therefore emits nothing; see the file comment.
			"function_statement": KindFunction,
			"function":           KindFunction,
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindMethod, KindConstructor, KindAccessor, KindClass, KindInterface,
				KindStruct, KindEnum, KindEnumMember, KindTrait, KindImpl,
				KindTypeAlias, KindProtocol, KindRecord, KindUnion, KindNamespace,
				KindMacro, KindAnnotation, KindDecorator, KindField, KindConstant,
			),
			map[string]string{
				KindModule: "a Lua module is a file returning a table; the return is not a declaration",
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang: "bash",
		ByNodeType: map[string]string{
			"function_definition": KindFunction,
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindMethod, KindConstructor, KindAccessor, KindClass, KindInterface,
				KindStruct, KindEnum, KindEnumMember, KindTrait, KindImpl,
				KindTypeAlias, KindProtocol, KindRecord, KindUnion, KindNamespace,
				KindModule, KindMacro, KindAnnotation, KindDecorator, KindField,
			),
			map[string]string{
				KindConstant: "`readonly` is a declaration_command modifier, not a constant declaration form the parser walks",
			},
		),
	})

	RegisterTaxonomy(&Taxonomy{
		Lang: "elixir",
		ByNodeType: map[string]string{
			"call": KindFunction,
		},
		Keywords: []KeywordKind{
			// Whole-word matching means `defmodule` never matches `def`.
			{NodeType: "call", Keyword: "defmodule", Kind: KindModule},
			{NodeType: "call", Keyword: "defprotocol", Kind: KindProtocol},
			{NodeType: "call", Keyword: "defimpl", Kind: KindImpl},
			{NodeType: "call", Keyword: "defstruct", Kind: KindStruct},
			{NodeType: "call", Keyword: "defmacro", Kind: KindMacro},
			{NodeType: "call", Keyword: "defmacrop", Kind: KindMacro},
		},
		Implements:      nil,
		OverrideMarkers: nil,
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindMethod, KindConstructor, KindAccessor, KindClass, KindInterface,
				KindEnum, KindEnumMember, KindTrait, KindRecord, KindUnion,
				KindAnnotation, KindDecorator, KindField,
			),
			map[string]string{
				KindTypeAlias: "@type is a module attribute, not a declaration node",
				KindNamespace: "Elixir spells the namespace construct `defmodule`, already mapped to module",
				KindConstant:  "a module attribute is not a constant declaration form",
			},
		),
	})
}
