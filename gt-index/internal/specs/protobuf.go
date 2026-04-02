package specs

import (
	"github.com/smacker/go-tree-sitter/protobuf"
)

func init() {
	Register(&Spec{
		Name:       "protobuf",
		Extensions: []string{".proto"},
		Language:   protobuf.GetLanguage(),

		FunctionNodes: []string{"rpc"},
		ClassNodes:    []string{"message", "service", "enum"},
		CallNodes:     []string{},
		ImportNodes:   []string{"import"},

		NameField:       "name",
		ReturnTypeField: "",
		BodyField:       "",
		ParamsField:     "",

		IsExported: func(name string) bool {
			return true
		},
	})
}
