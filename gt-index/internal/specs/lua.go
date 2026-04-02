package specs

import (
	"github.com/smacker/go-tree-sitter/lua"
)

func init() {
	Register(&Spec{
		Name:       "lua",
		Extensions: []string{".lua"},
		Language:   lua.GetLanguage(),

		FunctionNodes: []string{"function_declaration", "function_definition_statement"},
		ClassNodes:    []string{},
		CallNodes:     []string{"function_call"},
		ImportNodes:   []string{},

		NameField:   "name",
		BodyField:   "body",
		ParamsField: "parameters",

		IsExported: func(name string) bool {
			return true
		},
	})
}
