package specs

import (
	"github.com/smacker/go-tree-sitter/css"
)

func init() {
	Register(&Spec{
		Name:       "css",
		Extensions: []string{".css"},
		Language:   css.GetLanguage(),

		FunctionNodes: []string{},
		ClassNodes:    []string{"rule_set"},
		CallNodes:     []string{},
		ImportNodes:   []string{"import_statement"},

		NameField:       "",
		ReturnTypeField: "",
		BodyField:       "",
		ParamsField:     "",

		IsExported: func(name string) bool {
			return true
		},
	})
}
