package parser

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// ─────────────────────────────────────────────────────────────────────────────
// Indexer-fix batch — parser items:
//   #B3 File-anchor phantom nodes from comment text
//   #B4 field_read counting method calls
// ─────────────────────────────────────────────────────────────────────────────

func parseFixture(t *testing.T, name, src string) *ParseResult {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	spec := specs.ForExtension(filepath.Ext(name))
	if spec == nil {
		t.Skipf("no spec registered for %s", filepath.Ext(name))
	}
	sf := walker.SourceFile{Path: name, AbsPath: path, Language: spec.Name, Spec: spec}
	res, err := ParseFile(sf, false)
	if err != nil {
		t.Fatalf("ParseFile: %v", err)
	}
	return res
}

// #B3 RED→GREEN: a comment-only file whose COMMENTS contain module-link words
// (" from ", "import ") must NOT mint a synthetic File-anchor node. The old
// whole-text substring scan matched prose inside comments.
func TestFileAnchor_CommentTextDoesNotMintNode(t *testing.T) {
	src := "// This helper was copied from the upstream repo.\n" +
		"// import notes: keep in sync with v2.\n" +
		"/* export of this header is from the build step */\n"
	res := parseFixture(t, "notes.ts", src)
	for _, n := range res.Nodes {
		if n.Label == "File" {
			t.Errorf("comment-only file minted a phantom File-anchor node %q", n.Name)
		}
	}
}

// #B3 positive guard: a genuine barrel file (zero symbols, real re-export at
// line start) must still get its File anchor — the fix must not kill the
// legitimate anchoring that RE_EXPORTS edges depend on.
func TestFileAnchor_BarrelStillMintsNode(t *testing.T) {
	src := "export { Foo } from \"./foo\";\n" +
		"export * from \"./bar\";\n"
	res := parseFixture(t, "index.ts", src)
	var found bool
	for _, n := range res.Nodes {
		if n.Label == "File" {
			found = true
			if n.Name != "index" {
				t.Errorf("File anchor name = %q, want index", n.Name)
			}
		}
	}
	if !found {
		t.Error("barrel file with line-start re-exports lost its File anchor")
	}
}

// #B4 RED→GREEN (Python): the selector of a METHOD CALL (`self.area()`) must
// not be recorded as a field_read; a genuine read (`self.width`) must be.
// A chained call's receiver (`self.x` in `self.x.compute()`) is a genuine read
// and must survive.
func TestFieldRead_SkipsMethodCalls_Python(t *testing.T) {
	src := "class C:\n" +
		"    def f(self):\n" +
		"        self.area()\n" +
		"        v = self.x.compute()\n" +
		"        return self.width\n"
	res := parseFixture(t, "c.py", src)

	reads := map[string]bool{}
	for _, p := range res.Properties {
		if p.Kind == "field_read" {
			reads[p.Value] = true
		}
	}
	for v := range reads {
		if strings.Contains(v, "self.area") {
			t.Errorf("method call self.area() recorded as field_read: %q", v)
		}
	}
	var hasWidth, hasX bool
	for v := range reads {
		if strings.Contains(v, "self.width") {
			hasWidth = true
		}
		if strings.Contains(v, "self.x") {
			hasX = true
		}
	}
	if !hasWidth {
		t.Errorf("genuine field read self.width lost; reads=%v", reads)
	}
	if !hasX {
		t.Errorf("chained-call receiver read self.x lost; reads=%v", reads)
	}
}

// #B4 (Go): `c.Area()` (selector_expression under call_expression) must not be
// a field_read; `c.width` must be.
func TestFieldRead_SkipsMethodCalls_Go(t *testing.T) {
	src := "package x\n\n" +
		"type Circle struct{ width float64 }\n\n" +
		"func (c *Circle) Area() float64 { return 0 }\n\n" +
		"func (c *Circle) Scaled() float64 {\n" +
		"\treturn c.Area() * c.width\n" +
		"}\n"
	res := parseFixture(t, "circle.go", src)

	reads := map[string]bool{}
	for _, p := range res.Properties {
		if p.Kind == "field_read" {
			reads[p.Value] = true
		}
	}
	for v := range reads {
		if strings.Contains(v, "c.Area") {
			t.Errorf("method call c.Area() recorded as field_read: %q", v)
		}
	}
	var hasWidth bool
	for v := range reads {
		if strings.Contains(v, "c.width") {
			hasWidth = true
		}
	}
	if !hasWidth {
		t.Errorf("genuine field read c.width lost; reads=%v", reads)
	}
}
