// Package parser extracts definitions and calls from source files using tree-sitter.
package parser

import (
	"context"
	"os"
	"strings"

	sitter "github.com/smacker/go-tree-sitter"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// ParseResult holds the extracted data from one file.
type ParseResult struct {
	Nodes   []store.Node
	Calls   []CallRef
	Imports []ImportRef
}

// CallRef is a raw (unresolved) call reference.
type CallRef struct {
	CallerNodeIdx     int    // index into ParseResult.Nodes
	CalleeName        string // the function/method name being called (last component)
	CalleeQualified   string // full qualified name if available (e.g. "obj.method")
	Line              int
	File              string
}

// ImportRef is a parsed import statement — maps an imported name to its source module.
type ImportRef struct {
	ImportedName string // the symbol name being imported ("*" for wildcard/package imports)
	ModulePath   string // the module/file path (e.g., "os.path", "./utils", "fmt")
	File         string // the file containing this import statement
	Line         int
}

// ParseFile parses a single source file and extracts definitions + calls.
func ParseFile(sf walker.SourceFile, isTest bool) (*ParseResult, error) {
	src, err := os.ReadFile(sf.AbsPath)
	if err != nil {
		return nil, err
	}

	parser := sitter.NewParser()
	parser.SetLanguage(sf.Spec.Language)

	tree, err := parser.ParseCtx(context.Background(), nil, src)
	if err != nil {
		return nil, err
	}
	defer tree.Close()

	result := &ParseResult{}
	root := tree.RootNode()

	// Walk the AST to extract definitions and calls
	walkNode(root, sf, src, isTest, result, 0)

	return result, nil
}

func walkNode(node *sitter.Node, sf walker.SourceFile, src []byte, isTest bool, result *ParseResult, parentNodeIdx int) {
	spec := sf.Spec
	nodeType := node.Type()

	// Check for function definition
	if spec.IsFunctionNode(nodeType) {
		name := extractFieldText(node, spec.NameField, src)
		if name == "" {
			name = extractFirstIdentifier(node, src)
		}
		if name != "" {
			sig := extractSignature(node, src)
			retType := extractFieldText(node, spec.ReturnTypeField, src)

			n := store.Node{
				Label:      "Function",
				Name:       name,
				FilePath:   sf.Path,
				StartLine:  int(node.StartPoint().Row) + 1,
				EndLine:    int(node.EndPoint().Row) + 1,
				Signature:  sig,
				ReturnType: retType,
				IsExported: spec.IsExported != nil && spec.IsExported(name),
				IsTest:     isTest,
				Language:   sf.Language,
			}

			// Check if this is a method (inside a class)
			if parentNodeIdx > 0 {
				n.Label = "Method"
				n.ParentID = int64(parentNodeIdx)
			}

			idx := len(result.Nodes)
			result.Nodes = append(result.Nodes, n)

			// Extract calls from this function's body
			bodyNode := node.ChildByFieldName(spec.BodyField)
			if bodyNode != nil {
				extractCalls(bodyNode, sf, src, result, idx)
			}
			return // don't recurse into children (we already extracted from body)
		}
	}

	// Check for class definition
	if spec.IsClassNode(nodeType) {
		name := extractFieldText(node, spec.NameField, src)
		if name == "" {
			name = extractFirstIdentifier(node, src)
		}
		if name != "" {
			n := store.Node{
				Label:      "Class",
				Name:       name,
				FilePath:   sf.Path,
				StartLine:  int(node.StartPoint().Row) + 1,
				EndLine:    int(node.EndPoint().Row) + 1,
				IsExported: spec.IsExported != nil && spec.IsExported(name),
				IsTest:     isTest,
				Language:   sf.Language,
			}
			idx := len(result.Nodes)
			result.Nodes = append(result.Nodes, n)

			// Recurse into class body to find methods
			for i := 0; i < int(node.ChildCount()); i++ {
				child := node.Child(i)
				walkNode(child, sf, src, isTest, result, idx+1) // +1 because node IDs are 1-based in DB
			}
			return
		}
	}

	// Check for import statement
	if spec.IsImportNode(nodeType) {
		extractImports(node, sf, src, result)
		// Don't return — imports may contain nested nodes in some grammars
		return
	}

	// Recurse into children
	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		walkNode(child, sf, src, isTest, result, parentNodeIdx)
	}
}

func extractCalls(node *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, callerIdx int) {
	spec := sf.Spec

	if spec.IsCallNode(node.Type()) {
		simple, qualified := extractCalleeInfo(node, src)
		if simple != "" {
			result.Calls = append(result.Calls, CallRef{
				CallerNodeIdx:   callerIdx,
				CalleeName:      simple,
				CalleeQualified: qualified,
				Line:            int(node.StartPoint().Row) + 1,
				File:            sf.Path,
			})
		}
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		extractCalls(node.Child(i), sf, src, result, callerIdx)
	}
}

// extractCalleeInfo returns (simpleName, qualifiedName) for a call expression.
// simpleName is the last identifier (e.g. "baz" from "foo.bar.baz()").
// qualifiedName is the full dotted path (e.g. "foo.bar.baz").
func extractCalleeInfo(callNode *sitter.Node, src []byte) (string, string) {
	if callNode.ChildCount() == 0 {
		return "", ""
	}
	funcNode := callNode.Child(0)
	if funcNode == nil {
		return "", ""
	}

	// Direct call: foo(...)
	if funcNode.Type() == "identifier" {
		name := funcNode.Content(src)
		return name, name
	}

	// Method/attribute call: obj.method(...) or module.func(...)
	if funcNode.Type() == "attribute" || funcNode.Type() == "member_expression" ||
		funcNode.Type() == "selector_expression" || funcNode.Type() == "field_expression" {
		// Get the full qualified text
		qualified := funcNode.Content(src)

		// Get the simple name (last identifier)
		simpleName := ""
		for i := int(funcNode.ChildCount()) - 1; i >= 0; i-- {
			child := funcNode.Child(i)
			if child.Type() == "identifier" || child.Type() == "property_identifier" ||
				child.Type() == "field_identifier" {
				simpleName = child.Content(src)
				break
			}
		}
		if simpleName == "" {
			simpleName = qualified
		}
		return simpleName, qualified
	}

	content := funcNode.Content(src)
	return content, content
}

func extractFieldText(node *sitter.Node, fieldName string, src []byte) string {
	if fieldName == "" {
		return ""
	}
	child := node.ChildByFieldName(fieldName)
	if child == nil {
		return ""
	}
	return child.Content(src)
}

func extractFirstIdentifier(node *sitter.Node, src []byte) string {
	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child.Type() == "identifier" || child.Type() == "type_identifier" {
			return child.Content(src)
		}
	}
	return ""
}

func extractSignature(node *sitter.Node, src []byte) string {
	// Get the first line of the node as signature
	text := node.Content(src)
	if idx := strings.Index(text, "\n"); idx >= 0 {
		text = text[:idx]
	}
	if len(text) > 200 {
		text = text[:200]
	}
	return strings.TrimSpace(text)
}

// ── Import extraction ─────────────────────────────────────────────────────

// extractImports extracts import references from an import AST node.
// Language-agnostic: uses tree-sitter node types that vary by grammar.
func extractImports(node *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult) {
	lang := sf.Spec.Name
	line := int(node.StartPoint().Row) + 1

	switch lang {
	case "python":
		extractPythonImports(node, sf.Path, src, line, result)
	case "javascript", "typescript":
		extractJSTSImports(node, sf.Path, src, line, result)
	case "go":
		extractGoImports(node, sf.Path, src, line, result)
	case "java":
		extractJavaImports(node, sf.Path, src, line, result)
	case "rust":
		extractRustImports(node, sf.Path, src, line, result)
	}
}

// extractPythonImports handles:
//   - import_statement: "import os.path" → ImportRef{Name:"path", Module:"os.path"}
//   - import_from_statement: "from os.path import join, exists" → ImportRef{Name:"join", Module:"os.path"}, ...
func extractPythonImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	nodeType := node.Type()

	if nodeType == "import_from_statement" {
		// Get module name from "module_name" field or first dotted_name child
		modulePath := ""
		if mn := node.ChildByFieldName("module_name"); mn != nil {
			modulePath = mn.Content(src)
		} else {
			// Fallback: find dotted_name child
			for i := 0; i < int(node.ChildCount()); i++ {
				c := node.Child(i)
				if c.Type() == "dotted_name" {
					modulePath = c.Content(src)
					break
				}
			}
		}

		// Extract imported names
		for i := 0; i < int(node.ChildCount()); i++ {
			child := node.Child(i)
			switch child.Type() {
			case "dotted_name":
				// After "import" keyword — this is an imported name
				name := child.Content(src)
				// Skip if this is the module path (before "import" keyword)
				if name != modulePath && modulePath != "" {
					result.Imports = append(result.Imports, ImportRef{
						ImportedName: lastDotComponent(name),
						ModulePath:   modulePath,
						File:         file,
						Line:         line,
					})
				}
			case "aliased_import":
				// "from X import Y as Z" — extract the original name Y
				if nameNode := child.ChildByFieldName("name"); nameNode != nil {
					result.Imports = append(result.Imports, ImportRef{
						ImportedName: nameNode.Content(src),
						ModulePath:   modulePath,
						File:         file,
						Line:         line,
					})
				}
			case "identifier":
				text := child.Content(src)
				// Skip keywords: from, import, as
				if text != "from" && text != "import" && text != "as" && modulePath != "" {
					result.Imports = append(result.Imports, ImportRef{
						ImportedName: text,
						ModulePath:   modulePath,
						File:         file,
						Line:         line,
					})
				}
			case "wildcard_import":
				result.Imports = append(result.Imports, ImportRef{
					ImportedName: "*",
					ModulePath:   modulePath,
					File:         file,
					Line:         line,
				})
			}
		}
	} else if nodeType == "import_statement" {
		// "import os.path" or "import os.path as op"
		for i := 0; i < int(node.ChildCount()); i++ {
			child := node.Child(i)
			if child.Type() == "dotted_name" {
				fullPath := child.Content(src)
				result.Imports = append(result.Imports, ImportRef{
					ImportedName: lastDotComponent(fullPath),
					ModulePath:   fullPath,
					File:         file,
					Line:         line,
				})
			} else if child.Type() == "aliased_import" {
				if nameNode := child.ChildByFieldName("name"); nameNode != nil {
					fullPath := nameNode.Content(src)
					result.Imports = append(result.Imports, ImportRef{
						ImportedName: lastDotComponent(fullPath),
						ModulePath:   fullPath,
						File:         file,
						Line:         line,
					})
				}
			}
		}
	}
}

// extractJSTSImports handles:
//   - import_statement: "import { foo, bar } from './utils'" → ImportRef for each name
//   - Also handles: import X from './utils', import * as X from './utils'
func extractJSTSImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	// Get source path (the string literal after "from")
	sourceNode := node.ChildByFieldName("source")
	if sourceNode == nil {
		// Fallback: find the string child
		for i := 0; i < int(node.ChildCount()); i++ {
			c := node.Child(i)
			if c.Type() == "string" || c.Type() == "template_string" {
				sourceNode = c
				break
			}
		}
	}
	if sourceNode == nil {
		return
	}
	modulePath := stripQuotes(sourceNode.Content(src))
	if modulePath == "" {
		return
	}

	// Find named imports: import { foo, bar } from '...'
	foundNames := false
	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child.Type() == "import_clause" {
			extractJSImportClause(child, modulePath, file, src, line, result)
			foundNames = true
		} else if child.Type() == "named_imports" {
			extractJSNamedImports(child, modulePath, file, src, line, result)
			foundNames = true
		}
	}

	// If no named imports found, this might be a side-effect import
	if !foundNames {
		// Check for default import: import X from '...'
		for i := 0; i < int(node.ChildCount()); i++ {
			child := node.Child(i)
			if child.Type() == "identifier" {
				result.Imports = append(result.Imports, ImportRef{
					ImportedName: child.Content(src),
					ModulePath:   modulePath,
					File:         file,
					Line:         line,
				})
				foundNames = true
			}
		}
	}

	// Fallback: at minimum register a wildcard import for the module
	if !foundNames {
		result.Imports = append(result.Imports, ImportRef{
			ImportedName: "*",
			ModulePath:   modulePath,
			File:         file,
			Line:         line,
		})
	}
}

func extractJSImportClause(node *sitter.Node, modulePath, file string, src []byte, line int, result *ParseResult) {
	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		switch child.Type() {
		case "identifier":
			// Default import
			result.Imports = append(result.Imports, ImportRef{
				ImportedName: child.Content(src),
				ModulePath:   modulePath,
				File:         file,
				Line:         line,
			})
		case "named_imports":
			extractJSNamedImports(child, modulePath, file, src, line, result)
		case "namespace_import":
			// import * as X — wildcard
			result.Imports = append(result.Imports, ImportRef{
				ImportedName: "*",
				ModulePath:   modulePath,
				File:         file,
				Line:         line,
			})
		}
	}
}

func extractJSNamedImports(node *sitter.Node, modulePath, file string, src []byte, line int, result *ParseResult) {
	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child.Type() == "import_specifier" {
			// Named import: { foo } or { foo as bar }
			nameNode := child.ChildByFieldName("name")
			if nameNode == nil {
				nameNode = child.Child(0) // fallback: first child
			}
			if nameNode != nil && nameNode.Type() == "identifier" {
				result.Imports = append(result.Imports, ImportRef{
					ImportedName: nameNode.Content(src),
					ModulePath:   modulePath,
					File:         file,
					Line:         line,
				})
			}
		}
	}
}

// extractGoImports handles:
//   - import_declaration with import_spec children: import "fmt", import "os/path"
//   - Also import blocks: import ( "fmt" \n "os" )
func extractGoImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	// Walk children looking for import_spec nodes
	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child.Type() == "import_spec" || child.Type() == "import_spec_list" {
			extractGoImportSpec(child, file, src, result)
		}
	}
}

func extractGoImportSpec(node *sitter.Node, file string, src []byte, result *ParseResult) {
	if node.Type() == "import_spec_list" {
		// Block import: iterate children
		for i := 0; i < int(node.ChildCount()); i++ {
			extractGoImportSpec(node.Child(i), file, src, result)
		}
		return
	}

	if node.Type() != "import_spec" {
		return
	}

	// Get the path (interpreted_string_literal)
	pathNode := node.ChildByFieldName("path")
	if pathNode == nil {
		// Fallback: find string literal
		for i := 0; i < int(node.ChildCount()); i++ {
			c := node.Child(i)
			if c.Type() == "interpreted_string_literal" || c.Type() == "raw_string_literal" {
				pathNode = c
				break
			}
		}
	}
	if pathNode == nil {
		return
	}

	modulePath := stripQuotes(pathNode.Content(src))
	if modulePath == "" {
		return
	}

	// Go imports the entire package — use "*" as the imported name,
	// but also extract the package name (last path component)
	pkgName := lastSlashComponent(modulePath)
	line := int(node.StartPoint().Row) + 1

	// Check for alias: import alias "path"
	if nameNode := node.ChildByFieldName("name"); nameNode != nil {
		pkgName = nameNode.Content(src)
		if pkgName == "." {
			pkgName = "*" // dot import
		}
	}

	result.Imports = append(result.Imports, ImportRef{
		ImportedName: pkgName,
		ModulePath:   modulePath,
		File:         file,
		Line:         line,
	})
}

// extractJavaImports handles:
//   - import_declaration: "import com.foo.Bar;" → ImportRef{Name:"Bar", Module:"com.foo"}
//   - "import com.foo.*;" → ImportRef{Name:"*", Module:"com.foo"}
func extractJavaImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	// The import path is a scoped_identifier or identifier
	text := strings.TrimSpace(node.Content(src))
	// Remove "import " prefix and ";" suffix
	text = strings.TrimPrefix(text, "import ")
	text = strings.TrimPrefix(text, "static ")
	text = strings.TrimSuffix(text, ";")
	text = strings.TrimSpace(text)

	if text == "" {
		return
	}

	if strings.HasSuffix(text, ".*") {
		// Wildcard import
		modulePath := strings.TrimSuffix(text, ".*")
		result.Imports = append(result.Imports, ImportRef{
			ImportedName: "*",
			ModulePath:   modulePath,
			File:         file,
			Line:         line,
		})
	} else {
		// Named import: last dot component is the class name
		lastDot := strings.LastIndex(text, ".")
		if lastDot >= 0 {
			result.Imports = append(result.Imports, ImportRef{
				ImportedName: text[lastDot+1:],
				ModulePath:   text[:lastDot],
				File:         file,
				Line:         line,
			})
		} else {
			result.Imports = append(result.Imports, ImportRef{
				ImportedName: text,
				ModulePath:   "",
				File:         file,
				Line:         line,
			})
		}
	}
}

// extractRustImports handles:
//   - use_declaration: "use crate::foo::Bar;" → ImportRef{Name:"Bar", Module:"crate::foo"}
//   - "use std::collections::{HashMap, HashSet};" → multiple ImportRefs
func extractRustImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	text := strings.TrimSpace(node.Content(src))
	text = strings.TrimPrefix(text, "use ")
	text = strings.TrimSuffix(text, ";")
	text = strings.TrimSpace(text)

	if text == "" {
		return
	}

	// Handle use_list: use foo::{Bar, Baz}
	if braceStart := strings.Index(text, "{"); braceStart >= 0 {
		prefix := strings.TrimSuffix(text[:braceStart], "::")
		braceEnd := strings.Index(text, "}")
		if braceEnd > braceStart {
			items := strings.Split(text[braceStart+1:braceEnd], ",")
			for _, item := range items {
				name := strings.TrimSpace(item)
				// Handle "self" in use list
				if name == "self" {
					name = lastColonComponent(prefix)
				}
				if name != "" {
					result.Imports = append(result.Imports, ImportRef{
						ImportedName: name,
						ModulePath:   prefix,
						File:         file,
						Line:         line,
					})
				}
			}
		}
		return
	}

	// Handle glob: use foo::*
	if strings.HasSuffix(text, "::*") {
		modulePath := strings.TrimSuffix(text, "::*")
		result.Imports = append(result.Imports, ImportRef{
			ImportedName: "*",
			ModulePath:   modulePath,
			File:         file,
			Line:         line,
		})
		return
	}

	// Simple import: use foo::Bar or use foo::Bar as Baz
	// Handle alias
	if asIdx := strings.Index(text, " as "); asIdx >= 0 {
		text = text[:asIdx]
	}

	lastSep := strings.LastIndex(text, "::")
	if lastSep >= 0 {
		result.Imports = append(result.Imports, ImportRef{
			ImportedName: text[lastSep+2:],
			ModulePath:   text[:lastSep],
			File:         file,
			Line:         line,
		})
	} else {
		result.Imports = append(result.Imports, ImportRef{
			ImportedName: text,
			ModulePath:   "",
			File:         file,
			Line:         line,
		})
	}
}

// ── Helpers ───────────────────────────────────────────────────────────────

func lastDotComponent(s string) string {
	if idx := strings.LastIndex(s, "."); idx >= 0 {
		return s[idx+1:]
	}
	return s
}

func lastSlashComponent(s string) string {
	if idx := strings.LastIndex(s, "/"); idx >= 0 {
		return s[idx+1:]
	}
	return s
}

func lastColonComponent(s string) string {
	if idx := strings.LastIndex(s, "::"); idx >= 0 {
		return s[idx+2:]
	}
	return s
}

func stripQuotes(s string) string {
	if len(s) >= 2 {
		if (s[0] == '"' && s[len(s)-1] == '"') || (s[0] == '\'' && s[len(s)-1] == '\'') || (s[0] == '`' && s[len(s)-1] == '`') {
			return s[1 : len(s)-1]
		}
	}
	return s
}
