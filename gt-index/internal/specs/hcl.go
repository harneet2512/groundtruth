package specs

import (
	"github.com/smacker/go-tree-sitter/hcl"
)

func init() {
	Register(&Spec{
		Name:       "hcl",
		Extensions: []string{".tf", ".hcl"},
		Language:   hcl.GetLanguage(),

		FunctionNodes: []string{},
		ClassNodes:    []string{"block"},
		CallNodes:     []string{"function_call"},
		ImportNodes:   []string{},

		NameField:       "",
		ReturnTypeField: "",
		BodyField:       "",
		ParamsField:     "",

		IsExported: func(name string) bool {
			return true
		},
	})
}
