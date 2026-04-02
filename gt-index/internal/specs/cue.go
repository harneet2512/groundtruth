package specs

import (
	"github.com/smacker/go-tree-sitter/cue"
)

func init() {
	Register(&Spec{
		Name:       "cue",
		Extensions: []string{".cue"},
		Language:   cue.GetLanguage(),

		FunctionNodes: []string{},
		ClassNodes:    []string{},
		CallNodes:     []string{"call_expression"},
		ImportNodes:   []string{"import_declaration"},

		NameField:       "",
		ReturnTypeField: "",
		BodyField:       "",
		ParamsField:     "",

		IsExported: func(name string) bool {
			return true
		},
	})
}
