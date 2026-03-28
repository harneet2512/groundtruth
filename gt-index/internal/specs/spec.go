// Package specs defines language-specific tree-sitter node type mappings.
// The indexer core NEVER checks language names — all language-specific
// behavior lives here.
package specs

import (
	sitter "github.com/smacker/go-tree-sitter"
)

// Spec maps tree-sitter node types to GT's abstract schema for one language.
type Spec struct {
	Name       string
	Extensions []string

	// Tree-sitter node type names
	FunctionNodes []string // e.g. "function_definition" for Python
	ClassNodes    []string // e.g. "class_definition" for Python
	CallNodes     []string // e.g. "call" for Python
	ImportNodes   []string // e.g. "import_statement" for Python

	// Naming conventions
	TestFuncPattern   string   // regex for test function names
	AssertionPatterns []string // regex for assertion statements

	// Tree-sitter field names (vary by grammar)
	NameField       string // field containing the identifier name
	ReturnTypeField string // field containing return type annotation
	BodyField       string // field containing function body
	ParamsField     string // field containing parameters

	// Export detection
	IsExported func(name string) bool // language-specific export check

	// The tree-sitter Language object
	Language *sitter.Language
}

// Registry maps file extensions to language specs.
var Registry = map[string]*Spec{}

// Register adds a spec to the registry for all its extensions.
func Register(s *Spec) {
	for _, ext := range s.Extensions {
		Registry[ext] = s
	}
}

// ForExtension returns the spec for a file extension, or nil.
func ForExtension(ext string) *Spec {
	return Registry[ext]
}

// IsFunctionNode checks if a tree-sitter node type is a function definition.
func (s *Spec) IsFunctionNode(nodeType string) bool {
	for _, t := range s.FunctionNodes {
		if t == nodeType {
			return true
		}
	}
	return false
}

// IsClassNode checks if a tree-sitter node type is a class/struct definition.
func (s *Spec) IsClassNode(nodeType string) bool {
	for _, t := range s.ClassNodes {
		if t == nodeType {
			return true
		}
	}
	return false
}

// IsCallNode checks if a tree-sitter node type is a call expression.
func (s *Spec) IsCallNode(nodeType string) bool {
	for _, t := range s.CallNodes {
		if t == nodeType {
			return true
		}
	}
	return false
}

// IsImportNode checks if a tree-sitter node type is an import statement.
func (s *Spec) IsImportNode(nodeType string) bool {
	for _, t := range s.ImportNodes {
		if t == nodeType {
			return true
		}
	}
	return false
}
