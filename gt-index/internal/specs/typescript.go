package specs

import (
	typescript "github.com/smacker/go-tree-sitter/typescript/typescript"
)

func init() {
	Register(&Spec{
		Name:       "typescript",
		Extensions: []string{".ts", ".tsx"},
		Language:   typescript.GetLanguage(),

		FunctionNodes: []string{"function_declaration", "arrow_function", "method_definition"},
		ClassNodes:    []string{"class_declaration", "interface_declaration"},
		CallNodes:     []string{"call_expression"},
		ImportNodes:   []string{"import_statement"},

		TestFuncPattern: `^(test|it|describe)\b`,
		AssertionPatterns: []string{
			`expect\((.+?)\)\.(toBe|toEqual|toThrow)\((.+?)\)`,
		},

		NameField:       "name",
		ReturnTypeField: "return_type",
		BodyField:       "body",
		ParamsField:     "parameters",

		IsExported: func(name string) bool {
			return true // conservative
		},
	})
}
