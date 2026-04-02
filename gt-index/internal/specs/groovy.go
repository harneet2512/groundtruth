package specs

import (
	"unicode"

	// Groovy grammar is not available in go-tree-sitter; use Java grammar as fallback.
	java "github.com/smacker/go-tree-sitter/java"
)

func init() {
	Register(&Spec{
		Name:       "groovy",
		Extensions: []string{".groovy", ".gradle"},
		Language:   java.GetLanguage(),

		FunctionNodes: []string{"method_declaration"},
		ClassNodes:    []string{"class_declaration"},
		CallNodes:     []string{"method_call"},
		ImportNodes:   []string{"import_declaration"},

		NameField:   "name",
		BodyField:   "body",
		ParamsField: "formal_parameters",

		IsExported: func(name string) bool {
			return len(name) > 0 && unicode.IsUpper(rune(name[0]))
		},
	})
}
