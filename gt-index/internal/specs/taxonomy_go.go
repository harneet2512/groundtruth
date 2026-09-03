package specs

// Go. One node type, `type_declaration`, carries struct, interface and alias
// declarations, so the kind is refined from the node the grammar nests inside
// it. GT already promotes the interface case to its own label; the taxonomy
// adds the struct/alias distinction without touching any label.
//
// Go has no `implements` clause — conformance is structural and decided by the
// type checker, not the grammar — so IMPLEMENTS is declared unavailable here
// rather than guessed from an embedded type name.
func init() {
	RegisterTaxonomy(&Taxonomy{
		Lang: "go",
		ByNodeType: map[string]string{
			"function_declaration": KindFunction,
			"method_declaration":   KindMethod,
			// Default when the declaration nests none of the three below
			// (e.g. `type Celsius float64`): a named type, not a struct.
			"type_declaration": KindTypeAlias,
		},
		// Structural, not textual: the Go grammar nests `type_alias`,
		// `interface_type` or `struct_type` inside the declaration, so the
		// kind is read from the tree rather than guessed from the header.
		// Order matters — a `type_alias` node wraps whatever it aliases.
		ChildKinds: []ChildKind{
			{NodeType: "type_declaration", ChildType: "type_alias", Kind: KindTypeAlias},
			{NodeType: "type_declaration", ChildType: "interface_type", Kind: KindInterface},
			{NodeType: "type_declaration", ChildType: "struct_type", Kind: KindStruct},
		},
		Implements:      nil,
		OverrideMarkers: nil,
		AsProperty: map[string]string{
			KindField: "class_field",
		},
		Absent: mergeReasons(
			noDataKinds(),
			noMarkupKinds(),
			absent(ReasonNoConstruct,
				KindTrait, KindImpl, KindRecord, KindUnion, KindMacro,
				KindAnnotation, KindDecorator, KindProtocol, KindNamespace,
			),
			map[string]string{
				KindConstructor: "Go has no constructor declaration; `NewT` is a naming convention, not syntax",
				KindAccessor:    "Go has no accessor declaration; `GetX` is a naming convention, not syntax",
				KindClass:       "Go has no class; a named type is mapped to struct, interface or type_alias",
				KindEnum:        "Go has no enum; the iota idiom is a const_declaration indistinguishable from any other",
				KindEnumMember:  "no enum declaration, so no member declaration",
				KindModule:      "a Go module is a go.mod file, not a declaration inside a source file",
				KindConstant:    "const_declaration is not in the parser's declaration set; adding it would change no existing total but is out of this item's scope (see REPORT.md gaps)",
			},
		),
	})
}
