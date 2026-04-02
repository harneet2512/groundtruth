package specs

import (
	"github.com/smacker/go-tree-sitter/elixir"
)

func init() {
	Register(&Spec{
		Name:       "elixir",
		Extensions: []string{".ex", ".exs"},
		Language:   elixir.GetLanguage(),

		FunctionNodes: []string{"call"},
		ClassNodes:    []string{"call"},
		CallNodes:     []string{"call"},
		ImportNodes:   []string{},

		TestFuncPattern: `^test_`,

		NameField:   "",
		BodyField:   "",
		ParamsField: "arguments",

		IsExported: func(name string) bool {
			return true
		},
	})
}
