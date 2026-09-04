package specs

// Python. The grammar is deliberately thin on type declarations: an Enum is a
// class that inherits enum.Enum, a Protocol is a class that inherits
// typing.Protocol, a dataclass is a class with a decorator. None of those are
// syntactic facts about the DECLARATION — they are facts about a base-class
// NAME — so this mapping does not claim them. It records them absent with the
// reason, which is the point of writing the absences down.
func init() {
	RegisterTaxonomy(&Taxonomy{
		Lang: "python",
		ByNodeType: map[string]string{
			"function_definition": KindFunction,
			"class_definition":    KindClass,
		},
		ConstructorNames: []string{"__init__", "__new__"},
		Decls: map[string]Decl{
			// PEP 695 `type X = ...`. The pinned grammar exposes it as its own
			// declaration node, so it is a syntactic fact, not an assignment
			// that happens to look like one.
			"type_alias_statement": {Kind: KindTypeAlias, Label: "TypeAlias", NameField: "left"},
		},

		// Python has no `implements` clause and no `override` keyword.
		Implements:      nil,
		OverrideMarkers: nil,

		AsProperty: map[string]string{
			KindDecorator: "class_decorator",
			KindField:     "class_field",
		},
		Absent: mergeReasons(
			absent(ReasonNoSynthTest, KindTest),
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindInterface, KindStruct, KindTrait, KindImpl, KindRecord,
				KindUnion, KindMacro, KindAnnotation, KindNamespace,
			),
			map[string]string{
				KindMethod:     "a Python method is a function_definition inside a class body; the declaration node is identical to a module-level function, and GT already records the difference with the Method label and a parent",
				KindAccessor:   "@property decorates a function_definition; there is no accessor declaration node",
				KindEnum:       "an Enum is a class_definition inheriting enum.Enum; the declaration node is identical to any other class",
				KindEnumMember: "enum members are class-body assignments, indistinguishable from any other attribute",
				KindProtocol:   "typing.Protocol is a base-class name, not a declaration form",
				KindModule:     "a Python module is a file; GT already emits the File node",
				KindConstant:   "Python has no const declaration; module-level assignments are not distinguishable from variables",
			},
		),
	})
}
