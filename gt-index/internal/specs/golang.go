package specs

import (
	"unicode"

	golang "github.com/smacker/go-tree-sitter/golang"
)

func init() {
	Register(&Spec{
		Name:       "go",
		Extensions: []string{".go"},
		Language:   golang.GetLanguage(),

		FunctionNodes: []string{"function_declaration", "method_declaration"},
		ClassNodes:    []string{"type_declaration"},
		CallNodes:     []string{"call_expression"},
		ImportNodes:   []string{"import_declaration"},

		TestFuncPattern: `^Test`,
		AssertionPatterns: []string{
			`t\.(Error|Fatal|Fail)\w*\((.+)\)`,
			`assert\.\w+\((.+)\)`,
			`if\s+(.+?)\s*!=\s*(.+?)\s*\{`,
		},

		NameField:       "name",
		ReturnTypeField: "result",
		BodyField:       "body",
		ParamsField:     "parameters",

		IsExported: func(name string) bool {
			// Go: starts with uppercase letter
			if len(name) == 0 {
				return false
			}
			return unicode.IsUpper(rune(name[0]))
		},
	})
}
