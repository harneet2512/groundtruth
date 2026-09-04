package specs

import (
	"github.com/smacker/go-tree-sitter/sql"
)

func init() {
	Register(&Spec{
		Name:       "sql",
		Extensions: []string{".sql"},
		Language:   sql.GetLanguage(),

		FunctionNodes: []string{"create_function"},
		ClassNodes:    []string{},
		CallNodes:     []string{},
		ImportNodes:   []string{},

		NameField:       "name",
		ReturnTypeField: "",
		BodyField:       "body",
		ParamsField:     "",

		IsExported: func(name string) bool {
			return true
		},
	})
}
