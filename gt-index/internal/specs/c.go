package specs

import (
	"github.com/smacker/go-tree-sitter/c"
)

func init() {
	Register(&Spec{
		Name:       "c",
		Extensions: []string{".c", ".h"},
		Language:   c.GetLanguage(),

		FunctionNodes: []string{"function_definition"},
		ClassNodes:    []string{"struct_specifier"},
		CallNodes:     []string{"call_expression"},
		ImportNodes:   []string{"preproc_include"},

		TestFuncPattern: `^test_`,

		NameField:       "declarator",
		ReturnTypeField: "type",
		BodyField:       "body",
		ParamsField:     "parameters",

		IsExported: func(name string) bool {
			return true
		},
	})
}
