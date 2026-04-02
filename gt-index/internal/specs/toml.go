package specs

import (
	"github.com/smacker/go-tree-sitter/toml"
)

func init() {
	Register(&Spec{
		Name:       "toml",
		Extensions: []string{".toml"},
		Language:   toml.GetLanguage(),

		FunctionNodes: []string{},
		ClassNodes:    []string{"table"},
		CallNodes:     []string{},
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
