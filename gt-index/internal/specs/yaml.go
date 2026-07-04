package specs

import (
	"github.com/smacker/go-tree-sitter/yaml"
)

func init() {
	Register(&Spec{
		Name:       "yaml",
		Extensions: []string{".yml", ".yaml"},
		Language:   yaml.GetLanguage(),

		FunctionNodes: []string{},
		ClassNodes:    []string{"block_mapping"},
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
