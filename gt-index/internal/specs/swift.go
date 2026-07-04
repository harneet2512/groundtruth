package specs

import (
	"unicode"

	"github.com/smacker/go-tree-sitter/swift"
)

func init() {
	Register(&Spec{
		Name:       "swift",
		Extensions: []string{".swift"},
		Language:   swift.GetLanguage(),

		FunctionNodes: []string{"function_declaration"},
		ClassNodes:    []string{"class_declaration", "struct_declaration", "protocol_declaration"},
		CallNodes:     []string{"call_expression"},
		ImportNodes:   []string{"import_declaration"},

		NameField:       "name",
		ReturnTypeField: "return_type",
		BodyField:       "function_body",
		ParamsField:     "parameter_clause",

		IsExported: func(name string) bool {
			return len(name) > 0 && unicode.IsUpper(rune(name[0]))
		},
	})
}
