package parser

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

func TestCAndCppFunctionNamesExcludeSignatureText(t *testing.T) {
	for _, tc := range []struct{ ext, source string }{
		{".c", "int target(int value) { return value; }\n"},
		{".cpp", "int target(int value) { return value; }\n"},
	} {
		path := filepath.Join(t.TempDir(), "sample"+tc.ext)
		if err := os.WriteFile(path, []byte(tc.source), 0o600); err != nil {
			t.Fatal(err)
		}
		result, err := ParseFile(walker.SourceFile{Path: filepath.Base(path), AbsPath: path, Language: specs.ForExtension(tc.ext).Name, Spec: specs.ForExtension(tc.ext)}, false)
		if err != nil {
			t.Fatal(err)
		}
		if len(result.Nodes) == 0 || result.Nodes[0].Name != "target" {
			t.Fatalf("%s produced %+v", tc.ext, result.Nodes)
		}
	}
}
