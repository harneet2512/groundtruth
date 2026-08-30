package parser

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

func TestPythonImportRetainsSymbolWhenModuleHasSameName(t *testing.T) {
	path := filepath.Join(t.TempDir(), "caller.py")
	if err := os.WriteFile(path, []byte("from target import target\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	spec := specs.ForExtension(".py")
	result, err := ParseFile(walker.SourceFile{Path: "caller.py", AbsPath: path, Language: spec.Name, Spec: spec}, false)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, imp := range result.Imports {
		if imp.ModulePath == "target" && imp.ImportedName == "target" {
			found = true
		}
	}
	if !found {
		t.Fatalf("same-named module/symbol import was lost: %+v", result.Imports)
	}
}
