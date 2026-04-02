package specs

import (
	"github.com/smacker/go-tree-sitter/bash"
)

func init() {
	Register(&Spec{
		Name:       "bash",
		Extensions: []string{".sh", ".bash"},
		Language:   bash.GetLanguage(),

		FunctionNodes: []string{"function_definition"},
		ClassNodes:    []string{},
		CallNodes:     []string{"command"},
		ImportNodes:   []string{},

		NameField:   "name",
		BodyField:   "body",
		ParamsField: "",

		IsExported: func(name string) bool {
			return true
		},
	})
}
