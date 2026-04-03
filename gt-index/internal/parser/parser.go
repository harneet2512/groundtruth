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
	Nodes      []store.Node
	Calls      []CallRef
	Imports    []ImportRef
	Properties []PropertyRef
	Assertions []AssertionRef
}

// PropertyRef is a structural fact about a function node, extracted during parsing.
type PropertyRef struct {
	NodeIdx    int    // index into ParseResult.Nodes
	Kind       string // guard_clause, return_shape, exception_type, raise_type, docstring
	Value      string
	Line       int
	Confidence float64
}

// AssertionRef is an assertion extracted from a test function during parsing.
type AssertionRef struct {
	TestNodeIdx int    // index into ParseResult.Nodes (the test function)
	Kind        string // assertEqual, assertRaises, expect, assert, assert_eq, etc.
	Expression  string // readable assertion expression
	Expected    string // expected value if extractable
	Line        int
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

			// Extract properties (guard clauses, exception types, return shape)
			extractProperties(node, sf, src, result, idx)

			// Extract assertions from test functions
			if isTest {
				extractAssertionRefs(node, sf, src, result, idx)
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

	// JS/TS test frameworks: describe('name', () => { ... }), it('name', () => { ... }), test('name', fn)
	// These are call_expressions with a callback argument. We extract assertions from the callback body.
	if isTest && spec.IsCallNode(nodeType) && (sf.Language == "javascript" || sf.Language == "typescript") {
		simple, _ := extractCalleeInfo(node, src)
		if simple == "it" || simple == "test" || simple == "describe" {
			// Extract test name from first string argument
			testName := ""
			argsNode := node.ChildByFieldName("arguments")
			if argsNode != nil {
				for j := 0; j < int(argsNode.ChildCount()); j++ {
					arg := argsNode.Child(j)
					if arg.Type() == "string" || arg.Type() == "template_string" {
						testName = stripQuotes(strings.TrimSpace(arg.Content(src)))
						break
					}
				}
			}

			// Find callback argument (arrow_function or function_expression)
			if argsNode != nil {
				for j := 0; j < int(argsNode.ChildCount()); j++ {
					arg := argsNode.Child(j)
					argType := arg.Type()
					if argType == "arrow_function" || argType == "function" || argType == "function_expression" {
						// For "it"/"test" blocks: create a test function node and extract assertions
						if simple == "it" || simple == "test" {
							funcName := simple + ": " + testName
							if funcName == "" {
								funcName = simple
							}
							n := store.Node{
								Label:     "Function",
								Name:      funcName,
								FilePath:  sf.Path,
								StartLine: int(arg.StartPoint().Row) + 1,
								EndLine:   int(arg.EndPoint().Row) + 1,
								IsTest:    true,
								Language:  sf.Language,
							}
							idx := len(result.Nodes)
							result.Nodes = append(result.Nodes, n)

							// Extract calls from the callback body
							bodyNode := arg.ChildByFieldName("body")
							if bodyNode != nil {
								extractCalls(bodyNode, sf, src, result, idx)
								findAssertions(bodyNode, sf, src, result, idx, 0)
							} else {
								// Arrow function with expression body: () => expr
								extractCalls(arg, sf, src, result, idx)
								findAssertions(arg, sf, src, result, idx, 0)
							}
						}

						// For "describe" blocks: recurse into the callback to find nested it/test
						if simple == "describe" {
							bodyNode := arg.ChildByFieldName("body")
							if bodyNode != nil {
								for k := 0; k < int(bodyNode.ChildCount()); k++ {
									walkNode(bodyNode.Child(k), sf, src, true, result, parentNodeIdx)
								}
							}
						}
						break
					}
				}
			}
			return // handled
		}
	}

	// Recurse into children
	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		walkNode(child, sf, src, isTest, result, parentNodeIdx)
	}
}

func extractCalls(node *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, callerIdx int) {
	extractCallsWithParent(node, sf, src, result, callerIdx, "")
}

func extractCallsWithParent(node *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, callerIdx int, parentType string) {
	spec := sf.Spec
	nodeType := node.Type()

	if spec.IsCallNode(nodeType) {
		simple, qualified := extractCalleeInfo(node, src)
		if simple != "" {
			result.Calls = append(result.Calls, CallRef{
				CallerNodeIdx:   callerIdx,
				CalleeName:      simple,
				CalleeQualified: qualified,
				Line:            int(node.StartPoint().Row) + 1,
				File:            sf.Path,
			})

			// Classify caller usage context from parent node type
			usage := classifyCallContext(parentType, node, src)
			if usage != "" {
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx:    callerIdx,
					Kind:       "caller_usage",
					Value:      usage + ":" + simple,
					Line:       int(node.StartPoint().Row) + 1,
					Confidence: 0.8,
				})
			}
		}
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		extractCallsWithParent(node.Child(i), sf, src, result, callerIdx, nodeType)
	}
}

// classifyCallContext determines how a call's return value is used based on the parent AST node.
func classifyCallContext(parentType string, callNode *sitter.Node, src []byte) string {
	switch parentType {
	// Destructuring: a, b = func() / const {x, y} = func()
	case "assignment", "short_var_declaration", "variable_declaration":
		// Check if the assignment has multiple targets (tuple destructuring)
		// Simple heuristic: if there's a comma before the "=" on this line, it's a multi-assign
		lineText := ""
		if callNode.Parent() != nil {
			lineText = callNode.Parent().Content(src)
		}
		if strings.Contains(lineText, ",") && (strings.Contains(lineText, "=") || strings.Contains(lineText, ":=")) {
			return "destructure_tuple"
		}
		return ""

	// Iteration: for x := range func()
	case "for_statement", "for_in_statement", "for_in_clause", "for_clause":
		return "iterated"

	// Boolean check: if func() { ... }
	case "if_statement", "if_clause", "if_expression", "conditional_expression":
		return "boolean_check"

	// Exception guard: try { func() } catch / except
	case "try_statement", "try_expression":
		return "exception_guard"
	}
	return ""
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
	case "java", "kotlin", "groovy":
		extractJavaImports(node, sf.Path, src, line, result)
	case "scala":
		extractScalaImports(node, sf.Path, src, line, result)
	case "rust":
		extractRustImports(node, sf.Path, src, line, result)
	case "csharp":
		extractCSharpImports(node, sf.Path, src, line, result)
	case "php":
		extractPHPImports(node, sf.Path, src, line, result)
	case "c", "cpp":
		extractCCppImports(node, sf.Path, src, line, result)
	case "swift":
		extractSwiftImports(node, sf.Path, src, line, result)
	case "ocaml":
		extractOCamlImports(node, sf.Path, src, line, result)
	case "ruby":
		extractRubyImports(node, sf.Path, src, line, result)
	case "elixir":
		extractElixirImports(node, sf.Path, src, line, result)
	case "lua":
		extractLuaImports(node, sf.Path, src, line, result)
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

// ── Property & Assertion extraction ──────────────────────────────────────

// extractProperties extracts structural facts from a function AST node.
// Works across all languages by walking tree-sitter nodes generically.
func extractProperties(node *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, nodeIdx int) {
	bodyNode := node.ChildByFieldName(sf.Spec.BodyField)
	if bodyNode == nil {
		return
	}

	// Extract docstring (first string child of function, common in Python/JS/Go)
	extractDocstring(node, bodyNode, sf, src, result, nodeIdx)

	// Walk top-level statements in body for guard clauses and exception types
	for i := 0; i < int(bodyNode.ChildCount()); i++ {
		stmt := bodyNode.Child(i)
		stmtType := stmt.Type()

		// Guard clauses: if-raise/if-return/if-throw at the top of function body
		// Only first 5 statements count as "guards"
		if i < 5 {
			extractGuardFromStmt(stmt, stmtType, sf, src, result, nodeIdx)
		}

		// Exception types: raise/throw statements anywhere in body
		extractExceptionFromNode(stmt, sf, src, result, nodeIdx)
	}

	// Return shape: examine return statements
	extractReturnShape(bodyNode, sf, src, result, nodeIdx)
}

// extractDocstring extracts a docstring from a function node.
func extractDocstring(funcNode, bodyNode *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, nodeIdx int) {
	if bodyNode.ChildCount() == 0 {
		return
	}
	firstChild := bodyNode.Child(0)
	if firstChild == nil {
		return
	}
	childType := firstChild.Type()

	// Python: expression_statement containing a string
	if childType == "expression_statement" && firstChild.ChildCount() > 0 {
		inner := firstChild.Child(0)
		if inner != nil && inner.Type() == "string" {
			text := strings.TrimSpace(inner.Content(src))
			text = strings.Trim(text, `"'`)
			text = strings.Trim(text, "`")
			if len(text) > 200 {
				text = text[:200]
			}
			if text != "" {
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx:    nodeIdx,
					Kind:       "docstring",
					Value:      text,
					Line:       int(firstChild.StartPoint().Row) + 1,
					Confidence: 1.0,
				})
			}
		}
	}

	// JS/TS/Go: comment node before or inside function
	if childType == "comment" {
		text := strings.TrimSpace(firstChild.Content(src))
		text = strings.TrimLeft(text, "/*# ")
		text = strings.TrimRight(text, "*/# ")
		if len(text) > 200 {
			text = text[:200]
		}
		if text != "" {
			result.Properties = append(result.Properties, PropertyRef{
				NodeIdx:    nodeIdx,
				Kind:       "docstring",
				Value:      text,
				Line:       int(firstChild.StartPoint().Row) + 1,
				Confidence: 0.8,
			})
		}
	}
}

// extractGuardFromStmt checks if a statement is a guard clause (if-raise, if-return, if-throw).
func extractGuardFromStmt(stmt *sitter.Node, stmtType string, sf walker.SourceFile, src []byte, result *ParseResult, nodeIdx int) {
	if stmtType != "if_statement" && stmtType != "if_expression" {
		return
	}

	// Check if the body of the if contains a raise/throw/return
	text := stmt.Content(src)
	isGuard := false
	guardType := ""

	// Look for raise/throw/return in the if body
	for _, kw := range []string{"raise ", "throw ", "return", "panic(", "error(", "Error(", "abort("} {
		if strings.Contains(text, kw) {
			isGuard = true
			switch {
			case strings.Contains(text, "raise ") || strings.Contains(text, "throw "):
				guardType = "raise"
			case strings.Contains(text, "panic(") || strings.Contains(text, "abort("):
				guardType = "panic"
			default:
				guardType = "return"
			}
			break
		}
	}

	if isGuard {
		// Extract the condition from the if statement
		condNode := stmt.ChildByFieldName("condition")
		condText := ""
		if condNode != nil {
			condText = strings.TrimSpace(condNode.Content(src))
		}
		if condText == "" {
			// Fallback: take text between "if" and ":"/"{"
			condText = text
			if idx := strings.Index(condText, "{"); idx > 0 {
				condText = condText[3:idx]
			} else if idx := strings.Index(condText, ":"); idx > 0 {
				condText = condText[3:idx]
			}
			condText = strings.TrimSpace(condText)
		}
		if len(condText) > 120 {
			condText = condText[:120]
		}

		value := guardType + ": " + condText
		result.Properties = append(result.Properties, PropertyRef{
			NodeIdx:    nodeIdx,
			Kind:       "guard_clause",
			Value:      value,
			Line:       int(stmt.StartPoint().Row) + 1,
			Confidence: 1.0,
		})
	}
}

// extractExceptionFromNode recursively finds raise/throw/panic statements.
func extractExceptionFromNode(node *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, nodeIdx int) {
	nodeType := node.Type()

	// Match raise/throw/panic statements
	isException := false
	switch nodeType {
	case "raise_statement", "throw_statement", "throw_expression":
		isException = true
	case "expression_statement":
		// Check for panic() calls
		text := node.Content(src)
		if strings.Contains(text, "panic(") {
			isException = true
		}
	}

	if isException {
		text := strings.TrimSpace(node.Content(src))
		// Extract the exception type
		excType := ""
		switch {
		case strings.HasPrefix(text, "raise "):
			excType = strings.TrimPrefix(text, "raise ")
			if idx := strings.Index(excType, "("); idx > 0 {
				excType = excType[:idx]
			}
		case strings.HasPrefix(text, "throw "):
			excType = strings.TrimPrefix(text, "throw ")
			if strings.HasPrefix(excType, "new ") {
				excType = strings.TrimPrefix(excType, "new ")
			}
			if idx := strings.Index(excType, "("); idx > 0 {
				excType = excType[:idx]
			}
		case strings.Contains(text, "panic("):
			excType = "panic"
		default:
			excType = text
		}
		excType = strings.TrimSpace(excType)
		if len(excType) > 80 {
			excType = excType[:80]
		}
		if excType != "" {
			result.Properties = append(result.Properties, PropertyRef{
				NodeIdx:    nodeIdx,
				Kind:       "exception_type",
				Value:      excType,
				Line:       int(node.StartPoint().Row) + 1,
				Confidence: 1.0,
			})
		}
		return
	}

	// Recurse into children
	for i := 0; i < int(node.ChildCount()); i++ {
		extractExceptionFromNode(node.Child(i), sf, src, result, nodeIdx)
	}
}

// extractReturnShape classifies the return pattern of a function.
func extractReturnShape(bodyNode *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, nodeIdx int) {
	shapes := make(map[string]bool)
	countReturns(bodyNode, src, shapes)

	if len(shapes) == 0 {
		return
	}

	// Summarize
	for shape := range shapes {
		result.Properties = append(result.Properties, PropertyRef{
			NodeIdx:    nodeIdx,
			Kind:       "return_shape",
			Value:      shape,
			Line:       int(bodyNode.StartPoint().Row) + 1,
			Confidence: 0.9,
		})
	}
}

// countReturns recursively finds return statements and classifies their shape.
func countReturns(node *sitter.Node, src []byte, shapes map[string]bool) {
	if node.Type() == "return_statement" {
		text := strings.TrimSpace(node.Content(src))
		text = strings.TrimPrefix(text, "return ")
		text = strings.TrimSuffix(text, ";")
		text = strings.TrimSpace(text)

		switch {
		case text == "" || text == "return" || text == "None" || text == "nil" || text == "null" || text == "undefined":
			shapes["none"] = true
		case strings.HasPrefix(text, "(") && strings.Contains(text, ","):
			shapes["tuple"] = true
		case strings.HasPrefix(text, "[") || strings.HasPrefix(text, "{"):
			shapes["collection"] = true
		default:
			shapes["value"] = true
		}
		return
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		countReturns(node.Child(i), src, shapes)
	}
}

// extractAssertionRefs extracts assertions from test function bodies.
func extractAssertionRefs(funcNode *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, testNodeIdx int) {
	bodyNode := funcNode.ChildByFieldName(sf.Spec.BodyField)
	if bodyNode == nil {
		return
	}
	findAssertions(bodyNode, sf, src, result, testNodeIdx, 0)
}

// findAssertions recursively finds assertion calls in test function body.
func findAssertions(node *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, testNodeIdx int, depth int) {
	if depth > 10 { // prevent deep recursion
		return
	}

	nodeType := node.Type()

	// Match call expressions that look like assertions
	if sf.Spec.IsCallNode(nodeType) {
		simple, qualified := extractCalleeInfo(node, src)
		name := qualified
		if name == "" {
			name = simple
		}

		kind, isAssertion := classifyAssertion(name, simple)
		if isAssertion {
			text := strings.TrimSpace(node.Content(src))
			if len(text) > 200 {
				text = text[:200]
			}

			// Try to extract expected value from arguments
			expected := ""
			argsNode := node.ChildByFieldName("arguments")
			if argsNode != nil && argsNode.ChildCount() >= 3 {
				// Second argument is often the expected value (assertEqual(actual, expected))
				secondArg := argsNode.Child(2) // 0=open_paren, 1=first_arg, 2=comma or second_arg
				if secondArg != nil && secondArg.Type() != "," {
					expected = strings.TrimSpace(secondArg.Content(src))
					if len(expected) > 80 {
						expected = expected[:80]
					}
				}
			}

			result.Assertions = append(result.Assertions, AssertionRef{
				TestNodeIdx: testNodeIdx,
				Kind:        kind,
				Expression:  text,
				Expected:    expected,
				Line:        int(node.StartPoint().Row) + 1,
			})
			return // don't recurse into assertion args
		}
	}

	// Also match plain assert statements (Python: assert x == y)
	if nodeType == "assert_statement" || nodeType == "assert" {
		text := strings.TrimSpace(node.Content(src))
		if len(text) > 200 {
			text = text[:200]
		}
		result.Assertions = append(result.Assertions, AssertionRef{
			TestNodeIdx: testNodeIdx,
			Kind:        "assert",
			Expression:  text,
			Line:        int(node.StartPoint().Row) + 1,
		})
		return
	}

	// Also match Rust assert! and assert_eq! macros
	if nodeType == "macro_invocation" {
		text := node.Content(src)
		if strings.HasPrefix(text, "assert") {
			trimmed := strings.TrimSpace(text)
			if len(trimmed) > 200 {
				trimmed = trimmed[:200]
			}
			kind := "assert"
			if strings.HasPrefix(trimmed, "assert_eq!") {
				kind = "assert_eq"
			} else if strings.HasPrefix(trimmed, "assert_ne!") {
				kind = "assert_ne"
			}
			result.Assertions = append(result.Assertions, AssertionRef{
				TestNodeIdx: testNodeIdx,
				Kind:        kind,
				Expression:  trimmed,
				Line:        int(node.StartPoint().Row) + 1,
			})
			return
		}
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		findAssertions(node.Child(i), sf, src, result, testNodeIdx, depth+1)
	}
}

// classifyAssertion checks if a function call name is an assertion and returns its kind.
func classifyAssertion(qualified, simple string) (kind string, isAssertion bool) {
	// Normalize to lowercase for matching
	lowerSimple := strings.ToLower(simple)
	lowerQual := strings.ToLower(qualified)

	// Python unittest: self.assertEqual, self.assertRaises, etc.
	if strings.HasPrefix(lowerQual, "self.assert") {
		return simple, true
	}

	// Python pytest: pytest.raises
	if lowerQual == "pytest.raises" || strings.HasPrefix(lowerQual, "pytest.") {
		return simple, true
	}

	// Go testify: assert.Equal, require.NoError, etc.
	if strings.HasPrefix(lowerQual, "assert.") || strings.HasPrefix(lowerQual, "require.") {
		return simple, true
	}

	// Go testing.T methods: t.Error, t.Fatal, t.Fail, etc.
	if strings.HasPrefix(lowerQual, "t.") {
		switch lowerSimple {
		case "error", "errorf", "fatal", "fatalf", "fail", "failnow", "log", "logf":
			return simple, true
		}
	}

	// JS/TS expect().toBe() — the outer call is expect(), inner is method
	if lowerSimple == "expect" {
		return "expect", true
	}

	// Jest/Vitest matcher methods: expect(x).toBe(y), expect(x).toEqual(y), etc.
	if strings.HasPrefix(lowerSimple, "to") && strings.Contains(lowerQual, "expect") {
		return simple, true
	}
	// Jest matchers after .not: expect(x).not.toBe(y)
	if strings.HasPrefix(lowerSimple, "to") && strings.Contains(lowerQual, ".not.") {
		return simple, true
	}

	// JS/TS assert.strictEqual, assert.deepEqual, etc.
	if strings.HasPrefix(lowerQual, "assert.") {
		return simple, true
	}

	// C# Assert.AreEqual, Assert.That, etc.
	if strings.HasPrefix(qualified, "Assert.") {
		return simple, true
	}

	// JUnit/Kotlin: assertEquals, assertTrue, assertFalse, etc.
	if strings.HasPrefix(lowerSimple, "assert") && len(simple) > 6 {
		return simple, true
	}

	// PHP: $this->assertEquals, $this->assertSame, etc.
	if strings.Contains(lowerQual, "->assert") {
		return simple, true
	}

	// Ruby RSpec: expect(...).to, should, etc.
	if lowerSimple == "should" || lowerSimple == "expect" {
		return simple, true
	}

	// Swift: XCTAssertEqual, XCTAssertTrue, etc.
	if strings.HasPrefix(simple, "XCT") {
		return simple, true
	}

	return "", false
}

// extractScalaImports handles:
//   - import_declaration: "import com.foo.Bar" → ImportRef{Name:"Bar", Module:"com.foo"}
//   - "import com.foo.{Bar, Baz}" → multiple ImportRefs
//   - "import com.foo._" → ImportRef{Name:"*", Module:"com.foo"}
func extractScalaImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	text := strings.TrimSpace(node.Content(src))
	text = strings.TrimPrefix(text, "import ")
	text = strings.TrimSpace(text)

	if text == "" {
		return
	}

	// Handle brace imports: import com.foo.{Bar, Baz}
	if braceStart := strings.Index(text, "{"); braceStart >= 0 {
		prefix := strings.TrimSuffix(strings.TrimSpace(text[:braceStart]), ".")
		braceEnd := strings.Index(text, "}")
		if braceEnd > braceStart {
			items := strings.Split(text[braceStart+1:braceEnd], ",")
			for _, item := range items {
				name := strings.TrimSpace(item)
				// Handle rename: Bar => B
				if asIdx := strings.Index(name, "=>"); asIdx >= 0 {
					name = strings.TrimSpace(name[:asIdx])
				}
				if name == "_" {
					name = "*"
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

	// Wildcard: import com.foo._
	if strings.HasSuffix(text, "._") {
		modulePath := strings.TrimSuffix(text, "._")
		result.Imports = append(result.Imports, ImportRef{
			ImportedName: "*",
			ModulePath:   modulePath,
			File:         file,
			Line:         line,
		})
		return
	}

	// Simple import: import com.foo.Bar
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

// extractCSharpImports handles:
//   - using_directive: "using System.Collections.Generic;" → ImportRef{Name:"Generic", Module:"System.Collections"}
//   - "using Foo = System.IO;" → ImportRef{Name:"Foo", Module:"System.IO"} (alias)
func extractCSharpImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	text := strings.TrimSpace(node.Content(src))
	text = strings.TrimPrefix(text, "using ")
	text = strings.TrimPrefix(text, "static ")
	text = strings.TrimPrefix(text, "global::")
	text = strings.TrimSuffix(text, ";")
	text = strings.TrimSpace(text)

	if text == "" {
		return
	}

	// Handle alias: using Foo = System.IO
	if eqIdx := strings.Index(text, "="); eqIdx >= 0 {
		alias := strings.TrimSpace(text[:eqIdx])
		modulePath := strings.TrimSpace(text[eqIdx+1:])
		result.Imports = append(result.Imports, ImportRef{
			ImportedName: alias,
			ModulePath:   modulePath,
			File:         file,
			Line:         line,
		})
		return
	}

	// Standard: using System.Collections.Generic
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

// extractPHPImports handles:
//   - namespace_use_declaration: "use App\Http\Controllers\FooController;" → ImportRef
//   - "use App\Models\{User, Post};" → multiple ImportRefs
//   - "use App\Services\UserService as US;" → ImportRef with alias
func extractPHPImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	text := strings.TrimSpace(node.Content(src))
	text = strings.TrimPrefix(text, "use ")
	text = strings.TrimPrefix(text, "function ")
	text = strings.TrimPrefix(text, "const ")
	text = strings.TrimSuffix(text, ";")
	text = strings.TrimSpace(text)

	if text == "" {
		return
	}

	// Handle grouped imports: use App\Models\{User, Post}
	if braceStart := strings.Index(text, "{"); braceStart >= 0 {
		prefix := strings.TrimSuffix(strings.TrimSpace(text[:braceStart]), `\`)
		braceEnd := strings.Index(text, "}")
		if braceEnd > braceStart {
			items := strings.Split(text[braceStart+1:braceEnd], ",")
			for _, item := range items {
				name := strings.TrimSpace(item)
				// Handle alias: User as U
				if asIdx := strings.Index(name, " as "); asIdx >= 0 {
					name = strings.TrimSpace(name[:asIdx])
				}
				if name != "" {
					// Get the last component after any remaining backslash
					importName := name
					if lastBS := strings.LastIndex(name, `\`); lastBS >= 0 {
						importName = name[lastBS+1:]
					}
					result.Imports = append(result.Imports, ImportRef{
						ImportedName: importName,
						ModulePath:   prefix + `\` + strings.TrimSuffix(name, importName),
						File:         file,
						Line:         line,
					})
				}
			}
		}
		return
	}

	// Handle alias: use App\Services\UserService as US
	if asIdx := strings.Index(text, " as "); asIdx >= 0 {
		text = text[:asIdx]
	}

	// Standard: use App\Http\Controllers\FooController
	// Convert backslash to dot for module path
	lastBS := strings.LastIndex(text, `\`)
	if lastBS >= 0 {
		result.Imports = append(result.Imports, ImportRef{
			ImportedName: text[lastBS+1:],
			ModulePath:   text[:lastBS],
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

// extractCCppImports handles:
//   - preproc_include: '#include "path/file.h"' → ImportRef{Name:"file", Module:"path/file.h"}
//   - '#include <system/header.h>' → skipped (system headers)
//   - using_declaration (C++): 'using namespace std;' → ImportRef{Name:"*", Module:"std"}
func extractCCppImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	text := strings.TrimSpace(node.Content(src))
	nodeType := node.Type()

	if nodeType == "preproc_include" {
		// Only extract quoted includes (project-local), skip angle-bracket (system)
		if quoteStart := strings.Index(text, `"`); quoteStart >= 0 {
			quoteEnd := strings.LastIndex(text, `"`)
			if quoteEnd > quoteStart {
				path := text[quoteStart+1 : quoteEnd]
				name := lastSlashComponent(path)
				// Strip extension for the imported name
				if dotIdx := strings.LastIndex(name, "."); dotIdx >= 0 {
					name = name[:dotIdx]
				}
				result.Imports = append(result.Imports, ImportRef{
					ImportedName: name,
					ModulePath:   path,
					File:         file,
					Line:         line,
				})
			}
		}
		return
	}

	if nodeType == "using_declaration" {
		// using namespace std; → wildcard import
		text = strings.TrimPrefix(text, "using ")
		text = strings.TrimPrefix(text, "namespace ")
		text = strings.TrimSuffix(text, ";")
		text = strings.TrimSpace(text)
		if text != "" {
			result.Imports = append(result.Imports, ImportRef{
				ImportedName: "*",
				ModulePath:   text,
				File:         file,
				Line:         line,
			})
		}
	}
}

// extractSwiftImports handles:
//   - import_declaration: "import Foundation" → ImportRef{Name:"Foundation", Module:"Foundation"}
//   - "import struct Foundation.Date" → ImportRef{Name:"Date", Module:"Foundation"}
func extractSwiftImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	text := strings.TrimSpace(node.Content(src))
	text = strings.TrimPrefix(text, "import ")
	// Strip kind keywords: struct, class, enum, protocol, typealias, func, var, let
	for _, kw := range []string{"struct ", "class ", "enum ", "protocol ", "typealias ", "func ", "var ", "let "} {
		text = strings.TrimPrefix(text, kw)
	}
	text = strings.TrimSpace(text)

	if text == "" {
		return
	}

	// Sub-module import: Foundation.Date
	if lastDot := strings.LastIndex(text, "."); lastDot >= 0 {
		result.Imports = append(result.Imports, ImportRef{
			ImportedName: text[lastDot+1:],
			ModulePath:   text[:lastDot],
			File:         file,
			Line:         line,
		})
	} else {
		// Simple module import: import Foundation
		result.Imports = append(result.Imports, ImportRef{
			ImportedName: "*",
			ModulePath:   text,
			File:         file,
			Line:         line,
		})
	}
}

// extractOCamlImports handles:
//   - open_statement: "open Module_name" → ImportRef{Name:"*", Module:"Module_name"}
func extractOCamlImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	text := strings.TrimSpace(node.Content(src))
	text = strings.TrimPrefix(text, "open ")
	text = strings.TrimPrefix(text, "!")  // open! Module
	text = strings.TrimSpace(text)

	if text == "" {
		return
	}

	// OCaml open is always a wildcard — all module symbols become available
	result.Imports = append(result.Imports, ImportRef{
		ImportedName: "*",
		ModulePath:   text,
		File:         file,
		Line:         line,
	})
}

// extractRubyImports handles:
//   - require "module" → ImportRef{Name:"module", Module:"module"}
//   - require_relative "./foo" → ImportRef{Name:"foo", Module:"./foo"}
//
// Ruby's require/require_relative are method calls, so the ImportNodes spec
// uses "call". We filter by callee name here.
func extractRubyImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	text := strings.TrimSpace(node.Content(src))

	// Match: require "module" or require_relative "./module"
	for _, prefix := range []string{"require_relative ", "require "} {
		if strings.HasPrefix(text, prefix) {
			arg := strings.TrimPrefix(text, prefix)
			arg = stripQuotes(strings.TrimSpace(arg))
			if arg == "" {
				continue
			}
			name := lastSlashComponent(arg)
			result.Imports = append(result.Imports, ImportRef{
				ImportedName: name,
				ModulePath:   arg,
				File:         file,
				Line:         line,
			})
			return
		}
	}
}

// extractElixirImports handles:
//   - alias Module.Foo → ImportRef{Name:"Foo", Module:"Module.Foo"}
//   - import Module → ImportRef{Name:"*", Module:"Module"}
//   - use Module → ImportRef{Name:"*", Module:"Module"}
func extractElixirImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	text := strings.TrimSpace(node.Content(src))

	// alias Module.Foo
	if strings.HasPrefix(text, "alias ") {
		modPath := strings.TrimPrefix(text, "alias ")
		modPath = strings.TrimSpace(modPath)
		// Handle "alias Module.Foo, as: Bar"
		if commaIdx := strings.Index(modPath, ","); commaIdx >= 0 {
			modPath = strings.TrimSpace(modPath[:commaIdx])
		}
		name := lastDotComponent(modPath)
		result.Imports = append(result.Imports, ImportRef{
			ImportedName: name,
			ModulePath:   modPath,
			File:         file,
			Line:         line,
		})
		return
	}

	// import Module or use Module
	for _, kw := range []string{"import ", "use "} {
		if strings.HasPrefix(text, kw) {
			modPath := strings.TrimPrefix(text, kw)
			modPath = strings.TrimSpace(modPath)
			if commaIdx := strings.Index(modPath, ","); commaIdx >= 0 {
				modPath = strings.TrimSpace(modPath[:commaIdx])
			}
			if modPath != "" {
				result.Imports = append(result.Imports, ImportRef{
					ImportedName: "*",
					ModulePath:   modPath,
					File:         file,
					Line:         line,
				})
			}
			return
		}
	}
}

// extractLuaImports handles:
//   - require("module") → ImportRef{Name:"module", Module:"module"}
//   - require "module" → ImportRef{Name:"module", Module:"module"}
func extractLuaImports(node *sitter.Node, file string, src []byte, line int, result *ParseResult) {
	text := strings.TrimSpace(node.Content(src))

	if !strings.HasPrefix(text, "require") {
		return
	}

	// Extract the argument: require("foo") or require "foo" or require 'foo'
	arg := strings.TrimPrefix(text, "require")
	arg = strings.TrimSpace(arg)
	arg = strings.TrimPrefix(arg, "(")
	arg = strings.TrimSuffix(arg, ")")
	arg = stripQuotes(strings.TrimSpace(arg))

	if arg == "" {
		return
	}

	// Lua modules use dots: "lfs.path" → name is "path"
	name := arg
	if dotIdx := strings.LastIndex(arg, "."); dotIdx >= 0 {
		name = arg[dotIdx+1:]
	}

	result.Imports = append(result.Imports, ImportRef{
		ImportedName: name,
		ModulePath:   arg,
		File:         file,
		Line:         line,
	})
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
