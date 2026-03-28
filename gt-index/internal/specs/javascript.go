package specs

import (
	"github.com/smacker/go-tree-sitter/javascript"
)

func init() {
	Register(&Spec{
		Name:       "javascript",
		Extensions: []string{".js", ".jsx", ".mjs"},
		Language:   javascript.GetLanguage(),

		FunctionNodes: []string{"function_declaration", "arrow_function", "method_definition"},
		ClassNodes:    []string{"class_declaration"},
		CallNodes:     []string{"call_expression"},
		ImportNodes:   []string{"import_statement"},

		TestFuncPattern: `^(test|it|describe)\b`,
		AssertionPatterns: []string{
			`expect\((.+?)\)\.(toBe|toEqual|toThrow)\((.+?)\)`,
			`assert\.(equal|deepEqual|throws|ok)\((.+?)\)`,
		},

		NameField:       "name",
		ReturnTypeField: "",
		BodyField:       "body",
		ParamsField:     "parameters",

		IsExported: func(name string) bool {
			// JS: export keyword detected at AST level, not name-based
			return true // conservative: assume exported
		},
	})
}
