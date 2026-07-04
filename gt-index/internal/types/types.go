// Package types defines shared data structures used across parser, specs, and resolver.
//
// Deprecated: These types are duplicated in parser/parser.go (ImportRef, helper functions).
// No package in the codebase imports this package. It exists as a vestige of an earlier
// refactoring attempt. Safe to remove once confirmed no external consumers depend on it.
package types

// ImportRef is a parsed import statement — maps an imported name to its source module.
type ImportRef struct {
	ImportedName string // the symbol name being imported ("*" for wildcard/package imports)
	ModulePath   string // the module/file path (e.g., "os.path", "./utils", "fmt")
	File         string // the file containing this import statement
	Line         int
}
