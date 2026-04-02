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

		NameField: "",
		BodyField: "body",

		IsExported: func(name string) bool {
			return true
		},
	})
}
