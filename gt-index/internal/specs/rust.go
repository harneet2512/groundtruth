package specs

import (
	"github.com/smacker/go-tree-sitter/rust"
)

func init() {
	Register(&Spec{
		Name:       "rust",
		Extensions: []string{".rs"},
		Language:   rust.GetLanguage(),

		FunctionNodes: []string{"function_item"},
		ClassNodes:    []string{"struct_item", "impl_item", "enum_item", "trait_item"},
		CallNodes:     []string{"call_expression"},
		ImportNodes:   []string{"use_declaration"},

		TestFuncPattern: `^test_`,
		AssertionPatterns: []string{
			`assert(?:_eq)?!\((.+?)\)`,
			`#\[should_panic`,
		},

		NameField:       "name",
		ReturnTypeField: "return_type",
		BodyField:       "body",
		ParamsField:     "parameters",

		IsExported: func(name string) bool {
			// Rust: pub keyword detected at AST level
			return true // conservative
		},
	})
}
