package specs

import (
	markdown "github.com/smacker/go-tree-sitter/markdown/tree-sitter-markdown"
)

func init() {
	Register(&Spec{
		Name:       "markdown",
		Extensions: []string{".md"},
		Language:   markdown.GetLanguage(),

		FunctionNodes: []string{},
		ClassNodes:    []string{"section"},
		CallNodes:     []string{},
		ImportNodes:   []string{},

		NameField:       "",
		ReturnTypeField: "",
		BodyField:       "",
		ParamsField:     "",

		IsExported: func(name string) bool {
			return true
		},
	})
}
