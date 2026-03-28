package specs

import (
	"unicode"

	sitter "github.com/smacker/go-tree-sitter"
	"github.com/smacker/go-tree-sitter/python"
)

func init() {
	Register(&Spec{
		Name:       "python",
		Extensions: []string{".py"},
		Language:   python.GetLanguage(),

		FunctionNodes: []string{"function_definition"},
		ClassNodes:    []string{"class_definition"},
		CallNodes:     []string{"call"},
		ImportNodes:   []string{"import_statement", "import_from_statement"},

		TestFuncPattern: `^test_`,
		AssertionPatterns: []string{
			`assert\s+(.+)`,
			`self\.assert\w+\((.+)\)`,
			`pytest\.raises\((\w+)\)`,
		},

		NameField:       "name",
		ReturnTypeField: "return_type",
		BodyField:       "body",
		ParamsField:     "parameters",

		IsExported: func(name string) bool {
			// Python: not starting with underscore
			return len(name) > 0 && name[0] != '_'
		},
	})
}

// GetPythonLanguage returns the tree-sitter Python language for direct use.
func GetPythonLanguage() *sitter.Language {
	return python.GetLanguage()
}

// IsPythonTestFile checks if a file path is a Python test file.
func IsPythonTestFile(path string) bool {
	_ = unicode.IsUpper('A') // use unicode to avoid unused import
	return false              // handled by generic test detection
}
