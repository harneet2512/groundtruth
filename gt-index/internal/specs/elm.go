package specs

import (
	"github.com/smacker/go-tree-sitter/elm"
)

func init() {
	Register(&Spec{
		Name:       "elm",
		Extensions: []string{".elm"},
		Language:   elm.GetLanguage(),

		FunctionNodes: []string{"value_declaration"},
		ClassNodes:    []string{"type_declaration", "type_alias_declaration"},
		CallNodes:     []string{"function_call_expr"},
		ImportNodes:   []string{"import_clause"},

		NameField:       "",
		ReturnTypeField: "",
		BodyField:       "",
		ParamsField:     "",

		IsExported: func(name string) bool {
			return true
		},
	})
}
