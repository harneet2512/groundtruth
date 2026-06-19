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
		// "class" = class-EXPRESSION node (parity with javascript.go): captures
		// `export const X = class extends Base` whose inheritance was otherwise lost.
		ClassNodes:    []string{"class_declaration", "interface_declaration", "class"},
		CallNodes:     []string{"call_expression", "jsx_self_closing_element", "jsx_opening_element"},
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
