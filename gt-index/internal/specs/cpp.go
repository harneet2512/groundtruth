package specs

import (
	"github.com/smacker/go-tree-sitter/cpp"
)

func init() {
	Register(&Spec{
		Name:       "cpp",
		Extensions: []string{".cc", ".cpp", ".cxx", ".hpp", ".hxx"},
		Language:   cpp.GetLanguage(),

		FunctionNodes: []string{"function_definition"},
		ClassNodes:    []string{"class_specifier", "struct_specifier"},
		CallNodes:     []string{"call_expression"},
		ImportNodes:   []string{"preproc_include", "using_declaration"},

		NameField:       "declarator",
		ReturnTypeField: "type",
		BodyField:       "body",
		ParamsField:     "parameters",

		IsExported: func(name string) bool {
			return true
		},
	})
}
