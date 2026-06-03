package specs

import (
	"github.com/smacker/go-tree-sitter/javascript"
)

func init() {
	Register(&Spec{
		Name:       "javascript",
		Extensions: []string{".js", ".jsx", ".mjs", ".cjs"},
		Language:   javascript.GetLanguage(),

		FunctionNodes: []string{"function_declaration", "arrow_function", "method_definition"},
		// "class" is the class-EXPRESSION node (e.g. `module.exports = class App
		// extends Emitter` / `const X = class extends Base`) — without it the
		// indexer dropped every class assigned to an export/const, losing its
		// inheritance edge (koa Application extends Emitter -> 0 EXTENDS). The
		// parser skips anonymous (name=="") expressions, so this is safe.
		ClassNodes:    []string{"class_declaration", "class"},
		CallNodes:     []string{"call_expression", "jsx_self_closing_element", "jsx_opening_element"},
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
