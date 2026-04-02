package specs

import (
	"github.com/smacker/go-tree-sitter/ruby"
)

func init() {
	Register(&Spec{
		Name:       "ruby",
		Extensions: []string{".rb", ".rake"},
		Language:   ruby.GetLanguage(),

		FunctionNodes: []string{"method", "singleton_method"},
		ClassNodes:    []string{"class", "module"},
		CallNodes:     []string{"call", "method_call"},
		ImportNodes:   []string{},

		TestFuncPattern: `^test_`,

		NameField:   "name",
		BodyField:   "body",
		ParamsField: "parameters",

		IsExported: func(name string) bool {
			return len(name) > 0 && name[0] != '_'
		},
	})
}
