package parser

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

func parseDecoFixture(t *testing.T, name, ext, src string) *ParseResult {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte(src), 0o600); err != nil {
		t.Fatal(err)
	}
	spec := specs.ForExtension(ext)
	result, err := ParseFile(walker.SourceFile{Path: name, AbsPath: path, Language: spec.Name, Spec: spec}, false)
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func decoNodeIdxs(result *ParseResult) []int {
	var out []int
	for i := range result.Nodes {
		if result.Nodes[i].Label == "Decorator" {
			out = append(out, i)
		}
	}
	return out
}

func decoPropValues(result *ParseResult, nodeIdx int, kind string) []string {
	var out []string
	for _, p := range result.Properties {
		if p.NodeIdx == nodeIdx && p.Kind == kind {
			out = append(out, p.Value)
		}
	}
	return out
}

// A decorator occurrence must land as a real `Decorator` node parented to the
// decorated declaration, in every grammar placement: Python's
// decorated_definition children, TS decorator children, Java modifiers.
func TestDecoratorOccurrenceNodesAcrossGrammars(t *testing.T) {
	t.Run("python_function_stacked", func(t *testing.T) {
		result := parseDecoFixture(t, "a.py", ".py",
			"import functools\n\n@functools.lru_cache(maxsize=4)\n@logged\ndef cached(x):\n    return x\n")
		fnIdx := -1
		for i := range result.Nodes {
			if result.Nodes[i].Label == "Function" && result.Nodes[i].Name == "cached" {
				fnIdx = i
			}
		}
		if fnIdx < 0 {
			t.Fatal("function `cached` not emitted")
		}
		props := decoPropValues(result, fnIdx, "function_decorator")
		if len(props) != 2 {
			t.Fatalf("function_decorator props=%v, want 2", props)
		}
		idxs := decoNodeIdxs(result)
		if len(idxs) != 2 {
			t.Fatalf("Decorator nodes=%d, want 2", len(idxs))
		}
		// ParentID is the 1-based ordinal of `cached` at this stage.
		byName := map[string]int{}
		for _, di := range idxs {
			d := result.Nodes[di]
			if d.ParentID != int64(fnIdx+1) {
				t.Errorf("decorator %q ParentID=%d, want %d (cached)", d.Name, d.ParentID, fnIdx+1)
			}
			if d.IsExported {
				t.Errorf("decorator %q must not be exported", d.Name)
			}
			byName[d.Name] = di
		}
		if di, ok := byName["lru_cache"]; !ok || result.Nodes[di].QualifiedName != "functools.lru_cache" {
			t.Errorf("lru_cache occurrence wrong: %+v", result.Nodes[di])
		}
		if di, ok := byName["logged"]; !ok || result.Nodes[di].QualifiedName != "logged" {
			t.Errorf("logged occurrence wrong: %+v", result.Nodes[di])
		}
	})

	t.Run("typescript_class_child_placement", func(t *testing.T) {
		result := parseDecoFixture(t, "a.ts", ".ts",
			"@logged\nclass Store {\n  add(item: number): number { return 1; }\n}\n")
		clsIdx := -1
		for i := range result.Nodes {
			if result.Nodes[i].Label == "Class" && result.Nodes[i].Name == "Store" {
				clsIdx = i
			}
		}
		if clsIdx < 0 {
			t.Fatal("class `Store` not emitted")
		}
		if props := decoPropValues(result, clsIdx, "class_decorator"); len(props) != 1 || props[0] != "@logged" {
			t.Fatalf("class_decorator props=%v, want [@logged]", props)
		}
		idxs := decoNodeIdxs(result)
		if len(idxs) != 1 || result.Nodes[idxs[0]].Name != "logged" ||
			result.Nodes[idxs[0]].ParentID != int64(clsIdx+1) {
			t.Fatalf("Decorator nodes=%+v, want one `logged` parented to Store", idxs)
		}
	})

	t.Run("java_modifiers_placement", func(t *testing.T) {
		result := parseDecoFixture(t, "A.java", ".java",
			"@Logged\nclass Other {\n    @Override\n    public void run() {}\n}\n")
		clsIdx, mthIdx := -1, -1
		for i := range result.Nodes {
			if result.Nodes[i].Label == "Class" && result.Nodes[i].Name == "Other" {
				clsIdx = i
			}
			if result.Nodes[i].Label == "Method" && result.Nodes[i].Name == "run" {
				mthIdx = i
			}
		}
		if clsIdx < 0 || mthIdx < 0 {
			t.Fatalf("missing decls: cls=%d method=%d", clsIdx, mthIdx)
		}
		if props := decoPropValues(result, clsIdx, "class_decorator"); len(props) != 1 || props[0] != "@Logged" {
			t.Fatalf("class_decorator props=%v, want [@Logged]", props)
		}
		if props := decoPropValues(result, mthIdx, "function_decorator"); len(props) != 1 || props[0] != "@Override" {
			t.Fatalf("function_decorator props=%v, want [@Override]", props)
		}
		if idxs := decoNodeIdxs(result); len(idxs) != 2 {
			t.Fatalf("Decorator nodes=%d, want 2 (Logged + Override)", len(idxs))
		}
	})
}
