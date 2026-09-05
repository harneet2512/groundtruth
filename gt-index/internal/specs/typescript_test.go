package specs

import (
	"context"
	"testing"

	sitter "github.com/smacker/go-tree-sitter"
)

func TestTypeScriptDialectAndOverloadCompleteness(t *testing.T) {
	for _, tc := range []struct {
		name, extension, source string
		incomplete              bool
	}{
		{"newline generic overload", ".ts", "interface Attest {\n<const T>(actual: T): T\n<const T>(actual: T, expected: T): T\n}", false},
		{"explicit semicolons", ".ts", "interface Attest { <T>(x:T):T; <T>(x:T,y:T):T; }", false},
		{"jsx component", ".tsx", "export const App = () => <main><Child value={1}/></main>", false},
		{"jsx fragment", ".tsx", "export const App = () => <><Child /></>", false},
		{"tsx generic arrow", ".tsx", "const id = <T,>(x: T): T => x", false},
		{"type assertion", ".ts", "const value = <number>input", false},
		{"newline comparison", ".ts", "a\n<b", false},
		{"shift", ".ts", "a\n<<b", false},
		{"less equal", ".ts", "a\n<=b", false},
		{"broken overload", ".ts", "interface X { <T>(x: ): T }", true},
		{"unclosed jsx", ".tsx", "const x = <main><Child />", true},
		// Matching tag names is a semantic check, not tree-sitter parse completeness.
		{"tag mismatch is not a syntax guarantee", ".tsx", "const x = <main></other>", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			spec := ForExtension(tc.extension)
			if spec.Name != "typescript" {
				t.Fatalf("canonical language = %q", spec.Name)
			}
			parser := sitter.NewParser()
			defer parser.Close()
			parser.SetLanguage(spec.Language)
			tree, err := parser.ParseCtx(context.Background(), nil, []byte(tc.source))
			if err != nil {
				t.Fatal(err)
			}
			defer tree.Close()
			if tree.RootNode().HasError() != tc.incomplete {
				t.Fatalf("incomplete=%v, want %v: %s", tree.RootNode().HasError(), tc.incomplete, tree.RootNode().String())
			}
		})
	}
}
