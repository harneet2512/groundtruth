package specs

import (
	"github.com/smacker/go-tree-sitter/svelte"
)

func init() {
	Register(&Spec{
		Name:       "svelte",
		Extensions: []string{".svelte"},
		Language:   svelte.GetLanguage(),

		FunctionNodes: []string{},
		ClassNodes:    []string{},
		CallNodes:     []string{"call_expression"},
		ImportNodes:   []string{"import_statement"},

		NameField:       "name",
		ReturnTypeField: "",
		BodyField:       "body",
		ParamsField:     "parameters",

		IsExported: func(name string) bool {
			return true
		},
	})
}
