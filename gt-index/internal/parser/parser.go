// Package parser extracts definitions and calls from source files using tree-sitter.
package parser

import (
	"context"
	"os"
	"strings"

	sitter "github.com/smacker/go-tree-sitter"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// ParseResult holds the extracted data from one file.
type ParseResult struct {
	Nodes []store.Node
	Calls []CallRef
}

// CallRef is a raw (unresolved) call reference.
type CallRef struct {
	CallerNodeIdx int    // index into ParseResult.Nodes
	CalleeName    string // the function/method name being called
	Line          int
	File          string
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

	// Recurse into children
	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		walkNode(child, sf, src, isTest, result, parentNodeIdx)
	}
}

func extractCalls(node *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, callerIdx int) {
	spec := sf.Spec

	if spec.IsCallNode(node.Type()) {
		callee := extractCalleeName(node, src)
		if callee != "" {
			result.Calls = append(result.Calls, CallRef{
				CallerNodeIdx: callerIdx,
				CalleeName:    callee,
				Line:          int(node.StartPoint().Row) + 1,
				File:          sf.Path,
			})
		}
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		extractCalls(node.Child(i), sf, src, result, callerIdx)
	}
}

func extractCalleeName(callNode *sitter.Node, src []byte) string {
	// Try to get the function being called
	// For most languages: first child is the function expression
	if callNode.ChildCount() == 0 {
		return ""
	}
	funcNode := callNode.Child(0)
	if funcNode == nil {
		return ""
	}

	// Direct call: foo(...)
	if funcNode.Type() == "identifier" {
		return funcNode.Content(src)
	}

	// Method call: obj.method(...) or module.func(...)
	// Look for the last identifier (the method name)
	if funcNode.Type() == "attribute" || funcNode.Type() == "member_expression" ||
		funcNode.Type() == "selector_expression" || funcNode.Type() == "field_expression" {
		// Get the attribute/member name (usually the last child or "attribute" field)
		for i := int(funcNode.ChildCount()) - 1; i >= 0; i-- {
			child := funcNode.Child(i)
			if child.Type() == "identifier" || child.Type() == "property_identifier" ||
				child.Type() == "field_identifier" {
				return child.Content(src)
			}
		}
	}

	return funcNode.Content(src)
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
