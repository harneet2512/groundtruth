package specs

// Rust. The parser already emits struct_item, impl_item, enum_item and
// trait_item as nodes, but collapses all four into the `Class` label. The
// taxonomy separates them without moving that label, and adds the four
// declarations the parser emits nothing for: type alias, module, union and
// macro.
//
// `impl Trait for Type` is the cleanest syntactic conformance statement in any
// grammar here: the trait is a named field of the impl node, so IMPLEMENTS is
// read, not inferred.
func init() {
	RegisterTaxonomy(&Taxonomy{
		Lang: "rust",
		ByNodeType: map[string]string{
			"function_item":           KindFunction,
			"function_signature_item": KindFunction,
			"struct_item":             KindStruct,
			"enum_item":               KindEnum,
			"trait_item":              KindTrait,
			"impl_item":               KindImpl,
		},
		Decls: map[string]Decl{
			"type_item":        {Kind: KindTypeAlias, Label: "TypeAlias", NameField: "name"},
			"mod_item":         {Kind: KindModule, Label: "Module", NameField: "name"},
			"union_item":       {Kind: KindUnion, Label: "Union", NameField: "name"},
			"macro_definition": {Kind: KindMacro, Label: "Macro", NameField: "name"},
			"const_item":       {Kind: KindConstant, Label: "Constant", NameField: "name"},
		},
		Implements: []ImplementsRule{
			{NodeType: "impl_item", TraitFrom: "trait", TypeField: "type"},
		},
		OverrideMarkers: nil,
		AsProperty: map[string]string{
			KindField: "class_field",
		},
		Absent: mergeReasons(
			absent(ReasonNoSynthTest, KindTest),
			noDataKinds(),
			noMarkupKinds(),
			map[string]string{
				KindMethod:      "Rust methods are function_item nodes inside an impl block; the parser re-parents them, and the declaration node is identical to a free function",
				KindConstructor: "Rust has no constructor declaration; `fn new` is a convention, not syntax",
				KindAccessor:    "Rust has no accessor declaration distinct from a function",
				KindClass:       "Rust has no class; the declaration forms are struct, enum, union and trait",
				KindInterface:   "Rust spells the interface construct `trait`",
				KindProtocol:    "Rust spells the protocol construct `trait`",
				KindRecord:      ReasonNoConstruct,
				KindEnumMember:  "enum_variant is inside enum_variant_list; emitting variants is out of this item's scope (see REPORT.md gaps)",
				KindNamespace:   "Rust spells the namespace construct `mod`",
				KindAnnotation:  "Rust attributes are not declarations of an annotation type",
				KindDecorator:   "attribute macros decorate items, but GT records no decorator property for Rust (see REPORT.md gaps)",
			},
		),
	})
}
