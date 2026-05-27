package specs

import (
	"github.com/smacker/go-tree-sitter/ocaml"
)

func init() {
	Register(&Spec{
		Name:       "ocaml",
		Extensions: []string{".ml", ".mli"},
		Language:   ocaml.GetLanguage(),

		FunctionNodes: []string{"value_definition", "let_binding"},
		ClassNodes:    []string{"type_definition", "module_definition"},
		CallNodes:     []string{"application"},
		ImportNodes:   []string{"open_statement"},

		// KNOWN LIMITATION: OCaml tree-sitter grammar does not expose a "name" field
		// on value_definition or let_binding nodes. Names are extracted via the
		// extractFirstIdentifier fallback in parser.go, which finds the first
		// identifier child node. This works for simple let bindings (let foo = ...)
		// but may fail for pattern-matching bindings (let (a, b) = ...).
		NameField: "",
		BodyField: "body",

		IsExported: func(name string) bool {
			return true
		},
	})
}
