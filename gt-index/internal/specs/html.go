package specs

import (
	"github.com/smacker/go-tree-sitter/html"
)

func init() {
	Register(&Spec{
		Name:       "html",
		Extensions: []string{".html", ".htm"},
		Language:   html.GetLanguage(),

		FunctionNodes: []string{},
		ClassNodes:    []string{"element"},
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
