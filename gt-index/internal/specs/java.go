package specs

import (
	"unicode"

	"github.com/smacker/go-tree-sitter/java"
)

func init() {
	Register(&Spec{
		Name:       "java",
		Extensions: []string{".java"},
		Language:   java.GetLanguage(),

		FunctionNodes: []string{"method_declaration", "constructor_declaration"},
		ClassNodes:    []string{"class_declaration", "interface_declaration", "enum_declaration"},
		CallNodes:     []string{"method_invocation"},
		ImportNodes:   []string{"import_declaration"},

		TestFuncPattern: `^test`,
		AssertionPatterns: []string{
			`assert(Equals|True|False|NotNull|Null|Throws)\((.+?)\)`,
			`assertThat\((.+?)\)`,
		},

		NameField:       "name",
		ReturnTypeField: "type",
		BodyField:       "body",
		ParamsField:     "formal_parameters",

		IsExported: func(name string) bool {
			// Java: public keyword, but also uppercase first letter convention
			return len(name) > 0 && unicode.IsUpper(rune(name[0]))
		},
	})
}
