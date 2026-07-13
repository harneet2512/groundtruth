package parser

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// S6 RED->GREEN: Python type-stub files (.pyi) must index as Python.
//
// Before: only ".py" was registered on the python Spec, so specs.ForExtension(".pyi")
// returned nil. The L6 per-edit reindex of a .pyi stub (matplotlib-28933/29007 ship many)
// then failed ("no language spec registered for extension .pyi") and the stub's symbols
// never entered graph.db. A .pyi stub is valid Python syntax (an ellipsis body `...` is a
// legal expression statement), so the python tree-sitter grammar parses it directly — the
// only gap was the missing extension alias.
//
// This test is mutation-sensitive: remove ".pyi" from python.go's Extensions and
// ForExtension(".pyi") goes nil -> the first assertion Fatals (it does NOT skip).

func TestPyiRegistersAsPython(t *testing.T) {
	spec := specs.ForExtension(".pyi")
	if spec == nil {
		t.Fatal(".pyi has no registered language spec; expected the Python spec (alias missing in python.go)")
	}
	if spec.Name != "python" {
		t.Fatalf(".pyi must map to the python spec; got %q", spec.Name)
	}
}

func TestPyiStubParsesAsPython(t *testing.T) {
	// A minimal 3-line type stub, as shipped under matplotlib's .pyi tree.
	src := "class C:\n" +
		"    def m(self, x: int) -> str: ...\n" +
		"def f(x: int) -> str: ...\n"

	spec := specs.ForExtension(".pyi")
	if spec == nil {
		t.Fatal(".pyi has no registered language spec (alias missing in python.go)")
	}

	dir := t.TempDir()
	path := filepath.Join(dir, "stub.pyi")
	if err := os.WriteFile(path, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	sf := walker.SourceFile{Path: "stub.pyi", AbsPath: path, Language: spec.Name, Spec: spec}
	res, err := ParseFile(sf, false)
	if err != nil {
		t.Fatalf("ParseFile(.pyi): %v", err)
	}

	found := false
	for _, n := range res.Nodes {
		if n.Name == "f" {
			found = true
			if n.Language != "python" {
				t.Errorf("node f indexed with language %q; want python", n.Language)
			}
		}
	}
	if !found {
		names := make([]string, 0, len(res.Nodes))
		for _, n := range res.Nodes {
			names = append(names, n.Name)
		}
		t.Fatalf("expected a node named %q from the .pyi stub; got nodes %v", "f", names)
	}
}
