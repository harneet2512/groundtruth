// Package parser extracts definitions and calls from source files using tree-sitter.
package parser

import (
	"context"
	"fmt"
	"os"
	"strings"

	sitter "github.com/smacker/go-tree-sitter"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
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

// PropertyRef is a structural fact about a function or class node, extracted during parsing.
type PropertyRef struct {
	NodeIdx    int // index into ParseResult.Nodes
	Kind       string
	// Kinds: guard_clause, return_shape, exception_type, docstring, caller_usage,
	//        conditional_return, side_effect, param, security_tag, exception_flow,
	//        exception_handler, fingerprint, field_read, boundary_condition,
	//        class_field, class_decorator
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
		// JS/TS fix: arrow functions assigned to variables have no name field.
		// The name lives on the parent variable_declarator node.
		// e.g. const handler = async (req, res) => {}
		if name == "" && nodeType == "arrow_function" {
			parent := node.Parent()
			if parent != nil && parent.Type() == "variable_declarator" {
				name = extractFieldText(parent, "name", src)
			}
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
		// Go fix: type_declaration wraps type_spec children.
		// The "name" field lives on type_spec, not type_declaration.
		if name == "" && nodeType == "type_declaration" {
			for i := 0; i < int(node.ChildCount()); i++ {
				child := node.Child(i)
				if child.Type() == "type_spec" {
					name = extractFieldText(child, spec.NameField, src)
					if name != "" {
						break
					}
				}
			}
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

			// Extract class decorators (above the class definition)
			extractClassDecorators(node, src, result, idx)

			// Extract class fields from class body
			classBody := node.ChildByFieldName(spec.BodyField)
			if classBody != nil {
				extractClassFields(classBody, src, result, idx)
			}

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
		// If this node type also matches CallNodes (e.g. Ruby "call", Lua "function_call"),
		// do NOT return — fall through so calls are still extracted from this subtree.
		if !spec.IsCallNode(nodeType) {
			return
		}
		// Fall through: node is both an import and a call node.
		// Import extraction already ran; now let normal recursion handle call extraction.
	}

	// JS/TS test frameworks: describe('name', () => { ... }), it('name', () => { ... }), test('name', fn)
	// These are call_expressions with a callback argument. We extract assertions from the callback body.
	if isTest && spec.IsCallNode(nodeType) && (sf.Language == "javascript" || sf.Language == "typescript" || sf.Language == "coffeescript") {
		simple, _ := extractCalleeInfo(node, src)
		if simple == "it" || simple == "test" || simple == "describe" {
			// Extract test name from first string argument
			testName := ""
			argsNode := node.ChildByFieldName("arguments")
			// Fallback: some grammars use different field names
			if argsNode == nil {
				for k := 0; k < int(node.ChildCount()); k++ {
					child := node.Child(k)
					if child.Type() == "arguments" || child.Type() == "argument_list" {
						argsNode = child
						break
					}
				}
			}
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
	// Covers: Go (assignment, short_var_declaration), JS/TS (variable_declaration,
	// variable_declarator, assignment_expression), Java (local_variable_declaration),
	// Rust (let_declaration), Python (assignment, augmented_assignment)
	case "assignment", "short_var_declaration", "variable_declaration",
		"variable_declarator", "assignment_expression", "augmented_assignment",
		"local_variable_declaration", "let_declaration":
		lineText := ""
		if callNode.Parent() != nil {
			lineText = callNode.Parent().Content(src)
		}
		if strings.Contains(lineText, ",") && (strings.Contains(lineText, "=") || strings.Contains(lineText, ":=") || strings.Contains(lineText, "let")) {
			return "destructure_tuple"
		}
		return ""

	// Iteration: for x := range func() / for (x of func()) / for x in func()
	case "for_statement", "for_in_statement", "for_in_clause", "for_clause",
		"for_of_statement", "enhanced_for_statement":
		return "iterated"

	// Boolean check: if func() / if (func())
	case "if_statement", "if_clause", "if_expression", "conditional_expression",
		"ternary_expression", "parenthesized_expression":
		return "boolean_check"

	// Exception guard: try { func() } catch / except
	case "try_statement", "try_expression", "try_with_resources_statement":
		return "exception_guard"

	// Argument to another call: func(other_func())
	case "arguments", "argument_list":
		return ""

	// Return: return func()
	case "return_statement":
		return ""
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

	// Rust-specific: detect ? operator usage (early return on error)
	// and Result<T,E>/Option<T> return types as properties
	if sf.Language == "rust" {
		bodyText := bodyNode.Content(src)
		// ? operator = implicit guard clause for Result/Option
		if strings.Contains(bodyText, "?") {
			qCount := strings.Count(bodyText, "?")
			result.Properties = append(result.Properties, PropertyRef{
				NodeIdx:    nodeIdx,
				Kind:       "guard_clause",
				Value:      fmt.Sprintf("return: ? operator (%d early returns)", qCount),
				Line:       int(bodyNode.StartPoint().Row) + 1,
				Confidence: 0.9,
			})
		}
		// .unwrap() / .expect() = potential panic points
		for _, method := range []string{".unwrap()", ".expect("} {
			if strings.Contains(bodyText, method) {
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx:    nodeIdx,
					Kind:       "exception_type",
					Value:      "panic via " + strings.TrimSuffix(method, "("),
					Line:       int(bodyNode.StartPoint().Row) + 1,
					Confidence: 0.85,
				})
				break
			}
		}
		// Return type: detect Result<T,E> or Option<T> from function signature
		sigNode := node.ChildByFieldName("return_type")
		if sigNode != nil {
			retText := sigNode.Content(src)
			if strings.Contains(retText, "Result") {
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx:    nodeIdx,
					Kind:       "return_shape",
					Value:      "Result<T,E>",
					Line:       int(node.StartPoint().Row) + 1,
					Confidence: 1.0,
				})
			} else if strings.Contains(retText, "Option") {
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx:    nodeIdx,
					Kind:       "return_shape",
					Value:      "Option<T>",
					Line:       int(node.StartPoint().Row) + 1,
					Confidence: 1.0,
				})
			}
		}
	}

	// ── New property extractors ──────────────────────────────────────────

	// Conditional returns: if/elif with return statements
	extractConditionalReturns(bodyNode, src, result, nodeIdx)

	// Side effects: self./this. attribute mutations
	extractSideEffects(bodyNode, src, result, nodeIdx)

	// Structured parameters: name, type, default
	extractStructuredParams(node, sf.Spec, src, result, nodeIdx)

	// Security tags: authentication/authorization keywords in function name/decorators
	extractSecurityTags(node, src, result, nodeIdx)

	// Exception flow: raise/throw inside conditional blocks
	extractExceptionFlow(bodyNode, src, result, nodeIdx)

	// Exception handlers: except/catch clauses
	extractExceptionHandlers(bodyNode, src, result, nodeIdx)

	// Function fingerprint: complexity proxy + unique call names
	extractFunctionFingerprint(bodyNode, src, result, nodeIdx)

	// Field reads: self.x/this.x attribute reads (not assignments)
	extractFieldReads(bodyNode, src, result, nodeIdx)

	// Boundary conditions: comparisons with len(), 0, None, null, nil, index access
	extractBoundaryConditions(bodyNode, src, result, nodeIdx)
}

// extractDocstring extracts a docstring from a function node.
// Checks: (1) preceding sibling comment (Go/Java/Rust/TS/C++), (2) first body child (Python/JS).
func extractDocstring(funcNode, bodyNode *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, nodeIdx int) {
	// Strategy 1: Check preceding sibling of the function node for doc comments.
	// In Go, Java, Rust, TS, C++, doc comments appear BEFORE the function.
	prevSibling := funcNode.PrevSibling()
	if prevSibling != nil && prevSibling.Type() == "comment" {
		text := _cleanComment(prevSibling.Content(src))
		if len(text) >= 5 {
			if len(text) > 200 {
				text = text[:200]
			}
			result.Properties = append(result.Properties, PropertyRef{
				NodeIdx:    nodeIdx,
				Kind:       "docstring",
				Value:      text,
				Line:       int(prevSibling.StartPoint().Row) + 1,
				Confidence: 1.0,
			})
			return
		}
	}

	// Strategy 1a-fallback: Check parent's prev sibling (for TS: export_statement > function)
	if prevSibling == nil || (prevSibling.Type() != "comment" && prevSibling.Type() != "block_comment") {
		parent := funcNode.Parent()
		if parent != nil {
			parentPrev := parent.PrevSibling()
			if parentPrev != nil && (parentPrev.Type() == "comment" || parentPrev.Type() == "block_comment") {
				text := _cleanComment(parentPrev.Content(src))
				if len(text) >= 5 {
					if len(text) > 200 {
						text = text[:200]
					}
					result.Properties = append(result.Properties, PropertyRef{
						NodeIdx:    nodeIdx,
						Kind:       "docstring",
						Value:      text,
						Line:       int(parentPrev.StartPoint().Row) + 1,
						Confidence: 0.9,
					})
					return
				}
			}
		}
	}

	// Strategy 1b: Check for multi-line block comment (Java /** */, C++ /** */)
	if prevSibling != nil && (prevSibling.Type() == "block_comment" || prevSibling.Type() == "line_comment") {
		text := _cleanComment(prevSibling.Content(src))
		if len(text) >= 5 {
			if len(text) > 200 {
				text = text[:200]
			}
			result.Properties = append(result.Properties, PropertyRef{
				NodeIdx:    nodeIdx,
				Kind:       "docstring",
				Value:      text,
				Line:       int(prevSibling.StartPoint().Row) + 1,
				Confidence: 1.0,
			})
			return
		}
	}

	if bodyNode.ChildCount() == 0 {
		return
	}
	firstChild := bodyNode.Child(0)
	if firstChild == nil {
		return
	}
	childType := firstChild.Type()

	// Strategy 2: Python — expression_statement containing a string (docstring)
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
			return
		}
	}

	// Strategy 3: comment node inside function body (fallback)
	if childType == "comment" {
		text := _cleanComment(firstChild.Content(src))
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

// _cleanComment strips comment markers from a comment string.
func _cleanComment(raw string) string {
	text := strings.TrimSpace(raw)
	// Block comments: /* ... */ or /** ... */
	text = strings.TrimPrefix(text, "/**")
	text = strings.TrimPrefix(text, "/*")
	text = strings.TrimSuffix(text, "*/")
	// Line comments: // or /// or #
	lines := strings.Split(text, "\n")
	var cleaned []string
	for _, line := range lines {
		line = strings.TrimSpace(line)
		line = strings.TrimPrefix(line, "///")
		line = strings.TrimPrefix(line, "//!")
		line = strings.TrimPrefix(line, "//")
		line = strings.TrimPrefix(line, "#")
		line = strings.TrimPrefix(line, "* ")
		line = strings.TrimPrefix(line, "*")
		line = strings.TrimSpace(line)
		if line != "" {
			cleaned = append(cleaned, line)
		}
	}
	return strings.Join(cleaned, " ")
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

	// Look for raise/throw/return/? operator in the if body
	for _, kw := range []string{"raise ", "throw ", "return", "panic(", "error(", "Error(", "abort(", "Err("} {
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

		// Extract the consequence body to show what happens when the guard fires.
		// Try "consequence" (Python) then "body" (Go/JS/Java/C).
		consequenceText := ""
		consNode := stmt.ChildByFieldName("consequence")
		if consNode == nil {
			consNode = stmt.ChildByFieldName("body")
		}
		if consNode != nil && consNode.ChildCount() > 0 {
			firstStmt := consNode.Child(0)
			if firstStmt != nil {
				consequenceText = strings.TrimSpace(firstStmt.Content(src))
				// Collapse multi-line to single line
				if nlIdx := strings.IndexByte(consequenceText, '\n'); nlIdx > 0 {
					consequenceText = consequenceText[:nlIdx]
				}
				if len(consequenceText) > 60 {
					consequenceText = consequenceText[:60]
				}
			}
		}

		value := guardType + ": " + condText
		if consequenceText != "" {
			value += " -> " + consequenceText
		}
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
	case "return_statement":
		// Go: return fmt.Errorf(...) or return errors.New(...)
		text := node.Content(src)
		if strings.Contains(text, "fmt.Errorf") || strings.Contains(text, "errors.New") ||
			strings.Contains(text, "errors.Wrap") || strings.Contains(text, "errors.Errorf") {
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
		case strings.Contains(text, "fmt.Errorf") || strings.Contains(text, "errors.New") ||
			strings.Contains(text, "errors.Wrap") || strings.Contains(text, "errors.Errorf"):
			excType = "error"
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

// ── New property extractors ─────────────────────────────────────────────────

// extractConditionalReturns finds if/elif blocks that contain return statements.
// Kind: conditional_return. Value: "if cond: return val" or "ELSE: return val".
func extractConditionalReturns(bodyNode *sitter.Node, src []byte, result *ParseResult, nodeIdx int) {
	_walkConditionalReturns(bodyNode, src, result, nodeIdx, 0)
}

func _walkConditionalReturns(node *sitter.Node, src []byte, result *ParseResult, nodeIdx int, depth int) {
	if depth > 10 {
		return
	}
	nodeType := node.Type()

	if nodeType == "if_statement" || nodeType == "elif_clause" || nodeType == "if_expression" {
		// Check for return_statement children inside the consequence/body
		consNode := node.ChildByFieldName("consequence")
		if consNode == nil {
			consNode = node.ChildByFieldName("body")
		}
		if consNode != nil {
			_findReturnsInBlock(consNode, node, src, result, nodeIdx, false)
		}
		// Check alternative (else)
		altNode := node.ChildByFieldName("alternative")
		if altNode != nil {
			if altNode.Type() == "else_clause" || altNode.Type() == "else" {
				_findReturnsInBlock(altNode, node, src, result, nodeIdx, true)
			} else if altNode.Type() == "elif_clause" || altNode.Type() == "if_statement" {
				// Recurse into elif
				_walkConditionalReturns(altNode, src, result, nodeIdx, depth+1)
			}
		}
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child == nil {
			continue
		}
		ct := child.Type()
		if ct == "if_statement" || ct == "elif_clause" || ct == "if_expression" {
			_walkConditionalReturns(child, src, result, nodeIdx, depth+1)
		}
	}
}

func _findReturnsInBlock(block *sitter.Node, ifNode *sitter.Node, src []byte, result *ParseResult, nodeIdx int, isElse bool) {
	for i := 0; i < int(block.ChildCount()); i++ {
		child := block.Child(i)
		if child == nil {
			continue
		}
		if child.Type() == "return_statement" {
			retText := strings.TrimSpace(child.Content(src))
			retText = strings.TrimPrefix(retText, "return ")
			retText = strings.TrimSuffix(retText, ";")
			retText = strings.TrimSpace(retText)
			if retText == "" {
				retText = "None"
			}

			var value string
			if isElse {
				value = fmt.Sprintf("ELSE: return %s", retText)
			} else {
				condNode := ifNode.ChildByFieldName("condition")
				condText := ""
				if condNode != nil {
					condText = strings.TrimSpace(condNode.Content(src))
				}
				if condText == "" {
					condText = "?"
				}
				value = fmt.Sprintf("if %s: return %s", condText, retText)
			}
			if len(value) > 200 {
				value = value[:197] + "..."
			}

			result.Properties = append(result.Properties, PropertyRef{
				NodeIdx:    nodeIdx,
				Kind:       "conditional_return",
				Value:      value,
				Line:       int(child.StartPoint().Row) + 1,
				Confidence: 1.0,
			})
		}
	}
}

// extractSideEffects finds assignment expressions where the left side starts with self. or this.
// Kind: side_effect. Value: "mutates: self.field_name".
func extractSideEffects(bodyNode *sitter.Node, src []byte, result *ParseResult, nodeIdx int) {
	_walkSideEffects(bodyNode, src, result, nodeIdx, 0)
}

func _walkSideEffects(node *sitter.Node, src []byte, result *ParseResult, nodeIdx int, depth int) {
	if depth > 15 {
		return
	}
	nodeType := node.Type()

	if nodeType == "assignment" || nodeType == "augmented_assignment" ||
		nodeType == "assignment_expression" || nodeType == "expression_statement" {
		if _tryExtractSideEffect(node, src, result, nodeIdx) {
			return
		}
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child != nil {
			_walkSideEffects(child, src, result, nodeIdx, depth+1)
		}
	}
}

// _tryExtractSideEffect checks if an assignment node mutates self./this. fields.
// Returns true if a side effect was found and emitted (caller should not recurse).
func _tryExtractSideEffect(node *sitter.Node, src []byte, result *ParseResult, nodeIdx int) bool {
	text := strings.TrimSpace(node.Content(src))
	// Check for self. or this. on the left side of =
	eqIdx := strings.Index(text, "=")
	if eqIdx < 0 {
		return false
	}
	// Avoid ==, !=, <=, >=
	if len(text) > eqIdx+1 && text[eqIdx+1] == '=' {
		return false
	}
	if eqIdx > 0 && (text[eqIdx-1] == '!' || text[eqIdx-1] == '<' || text[eqIdx-1] == '>') {
		return false
	}

	lhs := strings.TrimSpace(text[:eqIdx])

	if strings.HasPrefix(lhs, "self.") {
		field := strings.TrimPrefix(lhs, "self.")
		// Strip further attribute access (only first level)
		if dotIdx := strings.Index(field, "."); dotIdx > 0 {
			field = field[:dotIdx]
		}
		// Strip brackets
		if bIdx := strings.Index(field, "["); bIdx > 0 {
			field = field[:bIdx]
		}
		if field != "" {
			value := "mutates: self." + field
			if len(value) > 200 {
				value = value[:197] + "..."
			}
			result.Properties = append(result.Properties, PropertyRef{
				NodeIdx:    nodeIdx,
				Kind:       "side_effect",
				Value:      value,
				Line:       int(node.StartPoint().Row) + 1,
				Confidence: 1.0,
			})
		}
		return true
	}
	if strings.HasPrefix(lhs, "this.") || strings.HasPrefix(lhs, "this->") {
		sep := "."
		if strings.HasPrefix(lhs, "this->") {
			sep = "->"
		}
		field := lhs[len("this"+sep):]
		if dotIdx := strings.Index(field, "."); dotIdx > 0 {
			field = field[:dotIdx]
		}
		if bIdx := strings.Index(field, "["); bIdx > 0 {
			field = field[:bIdx]
		}
		if field != "" {
			value := "mutates: this." + field
			if len(value) > 200 {
				value = value[:197] + "..."
			}
			result.Properties = append(result.Properties, PropertyRef{
				NodeIdx:    nodeIdx,
				Kind:       "side_effect",
				Value:      value,
				Line:       int(node.StartPoint().Row) + 1,
				Confidence: 1.0,
			})
		}
		return true
	}
	return false
}

// extractStructuredParams extracts function parameters with type annotations and defaults.
// Kind: param. Value: "name:type [required]" or "name:type opt=default_value".
func extractStructuredParams(node *sitter.Node, spec *specs.Spec, src []byte, result *ParseResult, nodeIdx int) {
	paramsField := spec.ParamsField
	if paramsField == "" {
		return
	}
	paramsNode := node.ChildByFieldName(paramsField)
	if paramsNode == nil {
		return
	}

	for i := 0; i < int(paramsNode.ChildCount()); i++ {
		param := paramsNode.Child(i)
		if param == nil {
			continue
		}
		paramType := param.Type()

		// Skip punctuation: (, ), commas
		if paramType == "(" || paramType == ")" || paramType == "," ||
			paramType == "{" || paramType == "}" {
			continue
		}
		// Skip 'self' / 'cls' in Python
		if paramType == "identifier" {
			pText := param.Content(src)
			if pText == "self" || pText == "cls" {
				continue
			}
		}

		// Common param types across languages
		name := ""
		typeAnnotation := ""
		defaultVal := ""
		hasDefault := false

		switch paramType {
		case "identifier":
			// Plain parameter without type: e.g. Python `def f(x):`
			name = param.Content(src)

		case "typed_parameter", "typed_default_parameter":
			// Python: x: int or x: int = 5
			nameNode := param.ChildByFieldName("name")
			if nameNode != nil {
				name = nameNode.Content(src)
			}
			typeNode := param.ChildByFieldName("type")
			if typeNode != nil {
				typeAnnotation = typeNode.Content(src)
			}
			defNode := param.ChildByFieldName("value")
			if defNode != nil {
				defaultVal = defNode.Content(src)
				hasDefault = true
			}

		case "default_parameter":
			// Python: x=5 (no type)
			nameNode := param.ChildByFieldName("name")
			if nameNode != nil {
				name = nameNode.Content(src)
			}
			defNode := param.ChildByFieldName("value")
			if defNode != nil {
				defaultVal = defNode.Content(src)
				hasDefault = true
			}

		case "formal_parameter", "required_parameter", "optional_parameter":
			// JS/TS/Java: formal parameter
			// Try "name" field first, then "pattern"
			nameNode := param.ChildByFieldName("name")
			if nameNode == nil {
				nameNode = param.ChildByFieldName("pattern")
			}
			if nameNode != nil {
				name = nameNode.Content(src)
			}
			typeNode := param.ChildByFieldName("type")
			if typeNode != nil {
				typeAnnotation = typeNode.Content(src)
			}
			defNode := param.ChildByFieldName("value")
			if defNode != nil {
				defaultVal = defNode.Content(src)
				hasDefault = true
			}
			if paramType == "optional_parameter" {
				hasDefault = true
				if defaultVal == "" {
					defaultVal = "undefined"
				}
			}

		case "parameter_declaration", "parameter":
			// Go, Rust, C, etc.
			nameNode := param.ChildByFieldName("name")
			if nameNode == nil {
				nameNode = param.ChildByFieldName("pattern")
			}
			if nameNode != nil {
				name = nameNode.Content(src)
			}
			typeNode := param.ChildByFieldName("type")
			if typeNode != nil {
				typeAnnotation = typeNode.Content(src)
			}

		default:
			// Fallback: try extracting name field, then first identifier
			nameNode := param.ChildByFieldName("name")
			if nameNode != nil {
				name = nameNode.Content(src)
			} else {
				name = extractFirstIdentifier(param, src)
			}
			typeNode := param.ChildByFieldName("type")
			if typeNode != nil {
				typeAnnotation = typeNode.Content(src)
			}
		}

		if name == "" || name == "self" || name == "cls" {
			continue
		}

		var value string
		if typeAnnotation != "" {
			if hasDefault {
				if defaultVal != "" {
					value = fmt.Sprintf("%s:%s opt=%s", name, typeAnnotation, defaultVal)
				} else {
					value = fmt.Sprintf("%s:%s opt", name, typeAnnotation)
				}
			} else {
				value = fmt.Sprintf("%s:%s [required]", name, typeAnnotation)
			}
		} else {
			if hasDefault {
				if defaultVal != "" {
					value = fmt.Sprintf("%s opt=%s", name, defaultVal)
				} else {
					value = fmt.Sprintf("%s opt", name)
				}
			} else {
				value = fmt.Sprintf("%s [required]", name)
			}
		}
		if len(value) > 200 {
			value = value[:197] + "..."
		}

		result.Properties = append(result.Properties, PropertyRef{
			NodeIdx:    nodeIdx,
			Kind:       "param",
			Value:      value,
			Line:       int(param.StartPoint().Row) + 1,
			Confidence: 1.0,
		})
	}
}

// extractSecurityTags checks function name and decorator names for security-related keywords.
// Kind: security_tag. Value: "authentication: keyword_found" or "authorization: keyword_found".
func extractSecurityTags(node *sitter.Node, src []byte, result *ParseResult, nodeIdx int) {
	// Security keyword categories
	authenticationKW := []string{"auth", "login", "token", "password", "secret", "encrypt", "decrypt", "hash", "csrf"}
	authorizationKW := []string{"permission", "role", "sanitize", "validate_input"}

	// Check function name
	nameNode := node.ChildByFieldName("name")
	funcName := ""
	if nameNode != nil {
		funcName = strings.ToLower(nameNode.Content(src))
	}

	// Check decorators (Python: tree-sitter puts "decorator" as children before the function)
	decoratorNames := []string{}
	// Walk siblings before the function for decorator nodes
	prev := node.PrevSibling()
	for prev != nil && prev.Type() == "decorator" {
		decText := strings.ToLower(strings.TrimSpace(prev.Content(src)))
		decoratorNames = append(decoratorNames, decText)
		prev = prev.PrevSibling()
	}
	// Also check parent for decorators (some grammars nest function inside decorated_definition)
	parent := node.Parent()
	if parent != nil && parent.Type() == "decorated_definition" {
		for i := 0; i < int(parent.ChildCount()); i++ {
			child := parent.Child(i)
			if child != nil && child.Type() == "decorator" {
				decText := strings.ToLower(strings.TrimSpace(child.Content(src)))
				decoratorNames = append(decoratorNames, decText)
			}
		}
	}

	// Combine all text to search
	searchTexts := append([]string{funcName}, decoratorNames...)
	seen := make(map[string]bool)

	for _, text := range searchTexts {
		if text == "" {
			continue
		}
		for _, kw := range authenticationKW {
			if strings.Contains(text, kw) && !seen["authentication:"+kw] {
				seen["authentication:"+kw] = true
				value := "authentication: " + kw
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx:    nodeIdx,
					Kind:       "security_tag",
					Value:      value,
					Line:       int(node.StartPoint().Row) + 1,
					Confidence: 1.0,
				})
			}
		}
		for _, kw := range authorizationKW {
			if strings.Contains(text, kw) && !seen["authorization:"+kw] {
				seen["authorization:"+kw] = true
				value := "authorization: " + kw
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx:    nodeIdx,
					Kind:       "security_tag",
					Value:      value,
					Line:       int(node.StartPoint().Row) + 1,
					Confidence: 1.0,
				})
			}
		}
	}
}

// extractExceptionFlow finds raise/throw statements inside conditional blocks.
// Kind: exception_flow. Value: "WHEN cond: raise ExcType(msg)".
func extractExceptionFlow(bodyNode *sitter.Node, src []byte, result *ParseResult, nodeIdx int) {
	_walkExceptionFlow(bodyNode, src, result, nodeIdx, 0)
}

func _walkExceptionFlow(node *sitter.Node, src []byte, result *ParseResult, nodeIdx int, depth int) {
	if depth > 10 {
		return
	}
	nodeType := node.Type()

	if nodeType == "if_statement" || nodeType == "elif_clause" || nodeType == "if_expression" {
		condNode := node.ChildByFieldName("condition")
		condText := ""
		if condNode != nil {
			condText = strings.TrimSpace(condNode.Content(src))
		}
		if condText == "" {
			condText = "?"
		}
		if len(condText) > 80 {
			condText = condText[:80]
		}

		// Check consequence/body for raise/throw
		consNode := node.ChildByFieldName("consequence")
		if consNode == nil {
			consNode = node.ChildByFieldName("body")
		}
		if consNode != nil {
			_findRaisesInBlock(consNode, condText, src, result, nodeIdx)
		}
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child != nil {
			_walkExceptionFlow(child, src, result, nodeIdx, depth+1)
		}
	}
}

func _findRaisesInBlock(block *sitter.Node, condText string, src []byte, result *ParseResult, nodeIdx int) {
	for i := 0; i < int(block.ChildCount()); i++ {
		child := block.Child(i)
		if child == nil {
			continue
		}
		ct := child.Type()
		if ct == "raise_statement" || ct == "throw_statement" || ct == "throw_expression" {
			raiseText := strings.TrimSpace(child.Content(src))
			if len(raiseText) > 100 {
				raiseText = raiseText[:100]
			}
			value := fmt.Sprintf("WHEN %s: %s", condText, raiseText)
			if len(value) > 200 {
				value = value[:197] + "..."
			}
			result.Properties = append(result.Properties, PropertyRef{
				NodeIdx:    nodeIdx,
				Kind:       "exception_flow",
				Value:      value,
				Line:       int(child.StartPoint().Row) + 1,
				Confidence: 1.0,
			})
		}
		// Check for expression_statement containing panic()
		if ct == "expression_statement" {
			text := child.Content(src)
			if strings.Contains(text, "panic(") {
				raiseText := strings.TrimSpace(text)
				if len(raiseText) > 100 {
					raiseText = raiseText[:100]
				}
				value := fmt.Sprintf("WHEN %s: %s", condText, raiseText)
				if len(value) > 200 {
					value = value[:197] + "..."
				}
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx:    nodeIdx,
					Kind:       "exception_flow",
					Value:      value,
					Line:       int(child.StartPoint().Row) + 1,
					Confidence: 1.0,
				})
			}
		}
	}
}

// extractExceptionHandlers finds except/catch clauses.
// Kind: exception_handler. Value: "except ExcType as var:" or "catch (ExcType var)".
func extractExceptionHandlers(bodyNode *sitter.Node, src []byte, result *ParseResult, nodeIdx int) {
	_walkExceptionHandlers(bodyNode, src, result, nodeIdx, 0)
}

func _walkExceptionHandlers(node *sitter.Node, src []byte, result *ParseResult, nodeIdx int, depth int) {
	if depth > 10 {
		return
	}
	nodeType := node.Type()

	if nodeType == "except_clause" || nodeType == "catch_clause" || nodeType == "rescue" {
		text := strings.TrimSpace(node.Content(src))
		// Take only the first line (the clause header)
		if idx := strings.Index(text, "\n"); idx >= 0 {
			text = text[:idx]
		}
		text = strings.TrimSpace(text)
		// Strip trailing colon and braces
		text = strings.TrimSuffix(text, ":")
		text = strings.TrimSuffix(text, "{")
		text = strings.TrimSpace(text)
		if len(text) > 200 {
			text = text[:197] + "..."
		}
		if text != "" {
			result.Properties = append(result.Properties, PropertyRef{
				NodeIdx:    nodeIdx,
				Kind:       "exception_handler",
				Value:      text,
				Line:       int(node.StartPoint().Row) + 1,
				Confidence: 1.0,
			})
		}
		return // don't recurse inside the handler
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child != nil {
			_walkExceptionHandlers(child, src, result, nodeIdx, depth+1)
		}
	}
}

// extractFunctionFingerprint computes a complexity proxy from named child count and unique calls.
// Kind: fingerprint. Value: "complexity:N|calls:func1,func2,func3".
func extractFunctionFingerprint(bodyNode *sitter.Node, src []byte, result *ParseResult, nodeIdx int) {
	complexity := int(bodyNode.NamedChildCount())
	calls := make(map[string]bool)
	_collectCallNames(bodyNode, src, calls, 0)

	callNames := make([]string, 0, len(calls))
	for name := range calls {
		callNames = append(callNames, name)
	}
	// Sort for determinism — simple insertion sort to avoid importing sort
	for i := 1; i < len(callNames); i++ {
		for j := i; j > 0 && callNames[j] < callNames[j-1]; j-- {
			callNames[j], callNames[j-1] = callNames[j-1], callNames[j]
		}
	}

	callList := strings.Join(callNames, ",")
	if len(callList) > 150 {
		callList = callList[:147] + "..."
	}

	value := fmt.Sprintf("complexity:%d|calls:%s", complexity, callList)
	if len(value) > 200 {
		value = value[:197] + "..."
	}

	result.Properties = append(result.Properties, PropertyRef{
		NodeIdx:    nodeIdx,
		Kind:       "fingerprint",
		Value:      value,
		Line:       int(bodyNode.StartPoint().Row) + 1,
		Confidence: 0.9,
	})
}

func _collectCallNames(node *sitter.Node, src []byte, calls map[string]bool, depth int) {
	if depth > 15 {
		return
	}
	nodeType := node.Type()

	// Match common call node types across languages
	if nodeType == "call" || nodeType == "call_expression" || nodeType == "method_invocation" {
		if node.ChildCount() > 0 {
			funcChild := node.Child(0)
			if funcChild != nil {
				// Get the simple name
				name := ""
				fType := funcChild.Type()
				if fType == "identifier" {
					name = funcChild.Content(src)
				} else if fType == "attribute" || fType == "member_expression" ||
					fType == "selector_expression" || fType == "field_expression" {
					// Get last identifier
					for j := int(funcChild.ChildCount()) - 1; j >= 0; j-- {
						child := funcChild.Child(j)
						if child != nil && (child.Type() == "identifier" || child.Type() == "property_identifier" || child.Type() == "field_identifier") {
							name = child.Content(src)
							break
						}
					}
				}
				if name != "" {
					calls[name] = true
				}
			}
		}
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child != nil {
			_collectCallNames(child, src, calls, depth+1)
		}
	}
}

// extractFieldReads finds self.x / this.x attribute access NOT on the left side of assignment.
// Kind: field_read. Value: "reads: self.field_name".
func extractFieldReads(bodyNode *sitter.Node, src []byte, result *ParseResult, nodeIdx int) {
	seen := make(map[string]bool)
	_walkFieldReads(bodyNode, src, result, nodeIdx, seen, 0)
}

func _walkFieldReads(node *sitter.Node, src []byte, result *ParseResult, nodeIdx int, seen map[string]bool, depth int) {
	if depth > 15 {
		return
	}
	nodeType := node.Type()

	// Skip assignment left-hand sides: those are side_effects, not reads
	if nodeType == "assignment" || nodeType == "augmented_assignment" || nodeType == "assignment_expression" {
		// The left child is the LHS — skip it, only walk the RHS
		lhsNode := node.ChildByFieldName("left")
		rhsNode := node.ChildByFieldName("right")
		if rhsNode != nil {
			_walkFieldReads(rhsNode, src, result, nodeIdx, seen, depth+1)
		}
		// Also walk value field (Python augmented_assignment uses 'right')
		valNode := node.ChildByFieldName("value")
		if valNode != nil {
			_walkFieldReads(valNode, src, result, nodeIdx, seen, depth+1)
		}
		// Walk any non-LHS, non-RHS children (shouldn't matter much, but be safe)
		for i := 0; i < int(node.ChildCount()); i++ {
			child := node.Child(i)
			if child != nil && child != lhsNode && child != rhsNode && child != valNode {
				_walkFieldReads(child, src, result, nodeIdx, seen, depth+1)
			}
		}
		return
	}

	// attribute / member_expression nodes: check for self.x / this.x
	if nodeType == "attribute" || nodeType == "member_expression" {
		text := node.Content(src)
		prefix := ""
		if strings.HasPrefix(text, "self.") {
			prefix = "self."
		} else if strings.HasPrefix(text, "this.") {
			prefix = "this."
		} else if strings.HasPrefix(text, "this->") {
			prefix = "this->"
		}
		if prefix != "" {
			field := text[len(prefix):]
			// Strip further chained access
			if dotIdx := strings.Index(field, "."); dotIdx > 0 {
				field = field[:dotIdx]
			}
			if dotIdx := strings.Index(field, "->"); dotIdx > 0 {
				field = field[:dotIdx]
			}
			// Strip brackets / parens
			if bIdx := strings.Index(field, "["); bIdx > 0 {
				field = field[:bIdx]
			}
			if bIdx := strings.Index(field, "("); bIdx > 0 {
				field = field[:bIdx]
			}
			key := prefix + field
			// Normalize this-> to this.
			if prefix == "this->" {
				key = "this." + field
			}
			if field != "" && !seen[key] {
				seen[key] = true
				value := "reads: " + key
				if len(value) > 200 {
					value = value[:197] + "..."
				}
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx:    nodeIdx,
					Kind:       "field_read",
					Value:      value,
					Line:       int(node.StartPoint().Row) + 1,
					Confidence: 0.9,
				})
			}
			// Don't recurse further into this attribute access node
			return
		}
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child != nil {
			_walkFieldReads(child, src, result, nodeIdx, seen, depth+1)
		}
	}
}

// extractBoundaryConditions finds comparisons involving len(), 0, 1, -1, None, null, nil, array indexing.
// Kind: boundary_condition. Value: "length_check|len(items) > max" or "zero_check|x == 0".
func extractBoundaryConditions(bodyNode *sitter.Node, src []byte, result *ParseResult, nodeIdx int) {
	seen := make(map[string]bool)
	_walkBoundaryConditions(bodyNode, src, result, nodeIdx, seen, 0)
}

func _walkBoundaryConditions(node *sitter.Node, src []byte, result *ParseResult, nodeIdx int, seen map[string]bool, depth int) {
	if depth > 12 {
		return
	}
	nodeType := node.Type()

	if nodeType == "comparison_operator" || nodeType == "binary_expression" ||
		nodeType == "comparison_expression" {
		text := strings.TrimSpace(node.Content(src))
		if len(text) > 150 {
			text = text[:150]
		}

		category := ""
		switch {
		case strings.Contains(text, "len(") || strings.Contains(text, ".length") ||
			strings.Contains(text, ".size()") || strings.Contains(text, ".count()") ||
			strings.Contains(text, "len!(") || strings.Contains(text, ".len()"):
			category = "length_check"
		case _containsBoundaryLiteral(text, "None") || _containsBoundaryLiteral(text, "null") ||
			_containsBoundaryLiteral(text, "nil") || _containsBoundaryLiteral(text, "nullptr") ||
			strings.Contains(text, "is None") || strings.Contains(text, "is not None") ||
			strings.Contains(text, "== null") || strings.Contains(text, "!= null") ||
			strings.Contains(text, "== nil") || strings.Contains(text, "!= nil"):
			category = "null_check"
		case _containsBoundaryLiteral(text, "0") || _containsBoundaryLiteral(text, "-1"):
			category = "zero_check"
		case strings.Contains(text, "[0]") || strings.Contains(text, "[-1]") ||
			strings.Contains(text, "[1]"):
			category = "index_boundary"
		}

		if category != "" && !seen[category+"|"+text] {
			seen[category+"|"+text] = true
			value := category + "|" + text
			if len(value) > 200 {
				value = value[:197] + "..."
			}
			result.Properties = append(result.Properties, PropertyRef{
				NodeIdx:    nodeIdx,
				Kind:       "boundary_condition",
				Value:      value,
				Line:       int(node.StartPoint().Row) + 1,
				Confidence: 0.9,
			})
		}
		return // don't recurse into comparison sub-nodes
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child != nil {
			_walkBoundaryConditions(child, src, result, nodeIdx, seen, depth+1)
		}
	}
}

// _containsBoundaryLiteral checks if text contains a literal value that appears as a
// comparison operand (surrounded by spaces/operators), not as part of a variable name.
func _containsBoundaryLiteral(text, literal string) bool {
	idx := strings.Index(text, literal)
	if idx < 0 {
		return false
	}
	// Check it's not part of a longer identifier
	if idx > 0 {
		prev := text[idx-1]
		if (prev >= 'a' && prev <= 'z') || (prev >= 'A' && prev <= 'Z') || prev == '_' || (prev >= '0' && prev <= '9') {
			return false
		}
	}
	end := idx + len(literal)
	if end < len(text) {
		next := text[end]
		if (next >= 'a' && next <= 'z') || (next >= 'A' && next <= 'Z') || next == '_' || (next >= '0' && next <= '9') {
			return false
		}
	}
	return true
}

// extractClassFields finds assignment statements in class body that are NOT inside methods.
// Kind: class_field. Value: "name = CharField(max_length=100)" or "name: str".
// Called from walkNode for ClassNodes, not from extractProperties.
func extractClassFields(classBodyNode *sitter.Node, src []byte, result *ParseResult, nodeIdx int) {
	for i := 0; i < int(classBodyNode.ChildCount()); i++ {
		child := classBodyNode.Child(i)
		if child == nil {
			continue
		}
		ct := child.Type()

		// Skip method definitions and nested class definitions — we only want class-level fields
		if ct == "function_definition" || ct == "method_definition" || ct == "method_declaration" ||
			ct == "constructor_declaration" || ct == "class_definition" || ct == "class_declaration" ||
			ct == "decorated_definition" || ct == "comment" || ct == "block_comment" {
			continue
		}

		// Python: expression_statement containing assignment
		if ct == "expression_statement" {
			innerCount := int(child.ChildCount())
			for j := 0; j < innerCount; j++ {
				inner := child.Child(j)
				if inner == nil {
					continue
				}
				it := inner.Type()
				if it == "assignment" || it == "augmented_assignment" {
					text := strings.TrimSpace(inner.Content(src))
					if len(text) > 200 {
						text = text[:197] + "..."
					}
					if text != "" {
						result.Properties = append(result.Properties, PropertyRef{
							NodeIdx:    nodeIdx,
							Kind:       "class_field",
							Value:      text,
							Line:       int(inner.StartPoint().Row) + 1,
							Confidence: 1.0,
						})
					}
				}
			}
			continue
		}

		// Python type annotation: name: str (type node)
		if ct == "type" {
			text := strings.TrimSpace(child.Content(src))
			if len(text) > 200 {
				text = text[:197] + "..."
			}
			if text != "" {
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx:    nodeIdx,
					Kind:       "class_field",
					Value:      text,
					Line:       int(child.StartPoint().Row) + 1,
					Confidence: 1.0,
				})
			}
			continue
		}

		// Direct assignment at class body level (JS/TS class property)
		if ct == "assignment" || ct == "public_field_definition" || ct == "field_declaration" ||
			ct == "field_definition" {
			text := strings.TrimSpace(child.Content(src))
			if len(text) > 200 {
				text = text[:197] + "..."
			}
			if text != "" {
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx:    nodeIdx,
					Kind:       "class_field",
					Value:      text,
					Line:       int(child.StartPoint().Row) + 1,
					Confidence: 1.0,
				})
			}
			continue
		}
	}
}

// extractClassDecorators finds decorator nodes above the class definition.
// Kind: class_decorator. Value: "@dataclass" or "@pytest.fixture".
// Called from walkNode for ClassNodes.
func extractClassDecorators(classNode *sitter.Node, src []byte, result *ParseResult, nodeIdx int) {
	// Strategy 1: Check if parent is a decorated_definition (Python)
	parent := classNode.Parent()
	if parent != nil && parent.Type() == "decorated_definition" {
		for i := 0; i < int(parent.ChildCount()); i++ {
			child := parent.Child(i)
			if child == nil {
				continue
			}
			if child.Type() == "decorator" {
				text := strings.TrimSpace(child.Content(src))
				if len(text) > 200 {
					text = text[:197] + "..."
				}
				if text != "" {
					result.Properties = append(result.Properties, PropertyRef{
						NodeIdx:    nodeIdx,
						Kind:       "class_decorator",
						Value:      text,
						Line:       int(child.StartPoint().Row) + 1,
						Confidence: 1.0,
					})
				}
			}
		}
		return
	}

	// Strategy 2: Check preceding siblings for decorator nodes
	prev := classNode.PrevSibling()
	for prev != nil && prev.Type() == "decorator" {
		text := strings.TrimSpace(prev.Content(src))
		if len(text) > 200 {
			text = text[:197] + "..."
		}
		if text != "" {
			result.Properties = append(result.Properties, PropertyRef{
				NodeIdx:    nodeIdx,
				Kind:       "class_decorator",
				Value:      text,
				Line:       int(prev.StartPoint().Row) + 1,
				Confidence: 1.0,
			})
		}
		prev = prev.PrevSibling()
	}

	// Strategy 3: Java/Kotlin annotations (marker_annotation, annotation)
	prev = classNode.PrevSibling()
	for prev != nil {
		pt := prev.Type()
		if pt == "marker_annotation" || pt == "annotation" {
			text := strings.TrimSpace(prev.Content(src))
			if len(text) > 200 {
				text = text[:197] + "..."
			}
			if text != "" {
				result.Properties = append(result.Properties, PropertyRef{
					NodeIdx:    nodeIdx,
					Kind:       "class_decorator",
					Value:      text,
					Line:       int(prev.StartPoint().Row) + 1,
					Confidence: 1.0,
				})
			}
			prev = prev.PrevSibling()
		} else {
			break
		}
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
				// Find the second real argument by skipping punctuation
				// children (parens and commas). Tree-sitter argument_list
				// children are: [open_paren, arg1, comma, arg2, ...close_paren]
				argCount := 0
				for j := 0; j < int(argsNode.ChildCount()); j++ {
					child := argsNode.Child(j)
					ct := child.Type()
					if ct == "(" || ct == ")" || ct == "," {
						continue
					}
					argCount++
					if argCount == 2 {
						expected = strings.TrimSpace(child.Content(src))
						if len(expected) > 80 {
							expected = expected[:80]
						}
						break
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

	// C++ Google Test: EXPECT_EQ, ASSERT_EQ, EXPECT_TRUE, ASSERT_FALSE, etc.
	if strings.HasPrefix(simple, "EXPECT_") || strings.HasPrefix(simple, "ASSERT_") {
		return simple, true
	}

	// C++ Catch2: REQUIRE, CHECK, REQUIRE_FALSE, CHECK_THAT, etc.
	if simple == "REQUIRE" || simple == "CHECK" ||
		strings.HasPrefix(simple, "REQUIRE_") || strings.HasPrefix(simple, "CHECK_") {
		return simple, true
	}

	// C++ Boost.Test: BOOST_CHECK, BOOST_REQUIRE, BOOST_TEST, etc.
	if strings.HasPrefix(simple, "BOOST_") {
		return simple, true
	}

	// C++ Google Test: TEST, TEST_F, TEST_P (test case macros, not assertions but test markers)
	if simple == "TEST" || simple == "TEST_F" || simple == "TEST_P" || simple == "TEST_CASE" {
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
