package specs

import (
	"unicode"

	"github.com/smacker/go-tree-sitter/scala"
)

func init() {
	Register(&Spec{
		Name:       "scala",
		Extensions: []string{".scala", ".sc"},
		Language:   scala.GetLanguage(),

		FunctionNodes: []string{"function_definition", "val_definition"},
		ClassNodes:    []string{"class_definition", "object_definition", "trait_definition"},
		CallNodes:     []string{"call_expression"},
		ImportNodes:   []string{"import_declaration"},

		NameField:   "name",
		BodyField:   "body",
		ParamsField: "parameters",

		IsExported: func(name string) bool {
			return len(name) > 0 && unicode.IsUpper(rune(name[0]))
		},
	})
}
