package specs

import (
	"unicode"

	"github.com/smacker/go-tree-sitter/csharp"
)

func init() {
	Register(&Spec{
		Name:       "csharp",
		Extensions: []string{".cs"},
		Language:   csharp.GetLanguage(),

		FunctionNodes: []string{"method_declaration", "constructor_declaration"},
		ClassNodes:    []string{"class_declaration", "interface_declaration", "struct_declaration"},
		CallNodes:     []string{"invocation_expression"},
		ImportNodes:   []string{"using_directive"},

		NameField:       "name",
		ReturnTypeField: "type",
		BodyField:       "body",
		ParamsField:     "parameters",

		IsExported: func(name string) bool {
			return len(name) > 0 && unicode.IsUpper(rune(name[0]))
		},
	})
}
