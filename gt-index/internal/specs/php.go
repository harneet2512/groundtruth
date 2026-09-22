package specs

import (
	"github.com/smacker/go-tree-sitter/php"
)

func init() {
	Register(&Spec{
		Name:       "php",
		Extensions: []string{".php"},
		Language:   php.GetLanguage(),

		FunctionNodes: []string{"function_definition", "method_declaration"},
		ClassNodes:    []string{"class_declaration", "interface_declaration"},
		// scoped_call_expression = `Foo::bar()` / `self::bar()` static calls;
		// nullsafe_member_call_expression = `$x?->run()`.
		CallNodes: []string{"function_call_expression", "member_call_expression",
			"nullsafe_member_call_expression", "scoped_call_expression"},
		ImportNodes: []string{"namespace_use_declaration"},

		NameField:       "name",
		ReturnTypeField: "return_type",
		BodyField:       "body",
		ParamsField:     "formal_parameters",

		IsExported: func(name string) bool {
			return true
		},
	})
}
