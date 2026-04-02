package specs

import (
	"unicode"

	"github.com/smacker/go-tree-sitter/kotlin"
)

func init() {
	Register(&Spec{
		Name:       "kotlin",
		Extensions: []string{".kt", ".kts"},
		Language:   kotlin.GetLanguage(),

		FunctionNodes: []string{"function_declaration"},
		ClassNodes:    []string{"class_declaration", "object_declaration"},
		CallNodes:     []string{"call_expression"},
		ImportNodes:   []string{"import_header"},

		NameField:       "simple_identifier",
		ReturnTypeField: "",
		BodyField:       "function_body",
		ParamsField:     "function_value_parameters",

		IsExported: func(name string) bool {
			return len(name) > 0 && unicode.IsUpper(rune(name[0]))
		},
	})
}
