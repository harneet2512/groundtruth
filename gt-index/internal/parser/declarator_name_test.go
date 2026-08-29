package parser

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

func TestCAndCppFunctionNamesExcludeSignatureText(t *testing.T) {
	for _, fixture := range []struct {
		name string
		ext  string
		code string
	}{
		{name: "c", ext: ".c", code: "int target(int value) { return value + 1; }\n"},
		{name: "cpp", ext: ".cpp", code: "int target(int value) { return value + 1; }\n"},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "fixture"+fixture.ext)
			if err := os.WriteFile(path, []byte(fixture.code), 0o644); err != nil {
				t.Fatal(err)
			}
			spec := specs.ForExtension(fixture.ext)
			result, err := ParseFile(walker.SourceFile{
				Path: fixture.name + fixture.ext, AbsPath: path,
				Language: spec.Name, Spec: spec,
			}, false)
			if err != nil {
				t.Fatal(err)
			}
			if len(result.Nodes) != 1 {
				t.Fatalf("nodes = %d; want 1: %#v", len(result.Nodes), result.Nodes)
			}
			if result.Nodes[0].Name != "target" {
				t.Fatalf("function name = %q; want target", result.Nodes[0].Name)
			}
		})
	}
}
