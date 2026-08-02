// Package specs defines language-specific tree-sitter node type mappings.
// The indexer core NEVER checks language names — all language-specific
// behavior lives here.
package specs

import (
	"encoding/json"
	"fmt"
	"sort"

	sitter "github.com/smacker/go-tree-sitter"
)

const LanguageManifestSchema = "gt.language_manifest.v1"

// LanguageManifestEntry is the deterministic, JSON-safe public projection of a
// registered parser spec. Capability booleans describe the configured grammar
// surface only; they are not an analyzer-accuracy or completeness claim.
type LanguageManifestEntry struct {
	Name         string   `json:"name"`
	Extensions   []string `json:"extensions"`
	Definitions  bool     `json:"definitions"`
	Calls        bool     `json:"calls"`
	Imports      bool     `json:"imports"`
	Bodies       bool     `json:"bodies"`
	Parameters   bool     `json:"parameters"`
	ReturnTypes  bool     `json:"return_types"`
	TestPatterns bool     `json:"test_patterns"`
}

type LanguageManifestDocument struct {
	Schema    string                  `json:"schema"`
	Languages []LanguageManifestEntry `json:"languages"`
}

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
	if s == nil || s.Name == "" {
		panic("specs: cannot register a nil or unnamed language spec")
	}
	for _, ext := range s.Extensions {
		if ext == "" {
			panic(fmt.Sprintf("specs: language %q has an empty extension", s.Name))
		}
		if existing := Registry[ext]; existing != nil && existing != s {
			panic(fmt.Sprintf(
				"specs: extension %q is registered by both %q and %q",
				ext, existing.Name, s.Name,
			))
		}
		Registry[ext] = s
	}
}

// LanguageManifest returns the one public language inventory derived from the
// live registry. It never maintains a second hand-written language list.
func LanguageManifest() LanguageManifestDocument {
	byName := make(map[string]*Spec)
	for _, spec := range Registry {
		if prior := byName[spec.Name]; prior != nil && prior != spec {
			panic(fmt.Sprintf("specs: language name %q maps to multiple specs", spec.Name))
		}
		byName[spec.Name] = spec
	}
	names := make([]string, 0, len(byName))
	for name := range byName {
		names = append(names, name)
	}
	sort.Strings(names)
	languages := make([]LanguageManifestEntry, 0, len(names))
	for _, name := range names {
		spec := byName[name]
		extensions := append([]string(nil), spec.Extensions...)
		sort.Strings(extensions)
		languages = append(languages, LanguageManifestEntry{
			Name:         spec.Name,
			Extensions:   extensions,
			Definitions:  len(spec.FunctionNodes) > 0 || len(spec.ClassNodes) > 0,
			Calls:        len(spec.CallNodes) > 0,
			Imports:      len(spec.ImportNodes) > 0,
			Bodies:       spec.BodyField != "",
			Parameters:   spec.ParamsField != "",
			ReturnTypes:  spec.ReturnTypeField != "",
			TestPatterns: spec.TestFuncPattern != "" || len(spec.AssertionPatterns) > 0,
		})
	}
	return LanguageManifestDocument{
		Schema:    LanguageManifestSchema,
		Languages: languages,
	}
}

// CanonicalLanguageManifestJSON emits compact deterministic bytes suitable for
// hashing into a repository configuration binding.
func CanonicalLanguageManifestJSON() ([]byte, error) {
	return json.Marshal(LanguageManifest())
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
