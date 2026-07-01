package parser

import "testing"

// countClassNamed returns how many Class-labelled nodes carry the given name.
func countClassNamed(res *ParseResult, name string) int {
	n := 0
	for i := range res.Nodes {
		if res.Nodes[i].Label == "Class" && res.Nodes[i].Name == name {
			n++
		}
	}
	return n
}

// classIdx1 returns the 1-based index of the (single) Class node named `name`,
// or 0 if none/ambiguous.
func classIdx1(res *ParseResult, name string) int64 {
	var idx int64
	for i := range res.Nodes {
		if res.Nodes[i].Label == "Class" && res.Nodes[i].Name == name {
			if idx != 0 {
				return 0 // ambiguous
			}
			idx = int64(i + 1)
		}
	}
	return idx
}

// RED→GREEN (P2 parser.go): a const-only or empty `impl Foo {}` has ZERO child
// method nodes. The old "no method children ⇒ canonical struct" heuristic
// therefore misclassified such an impl as a struct definition, leaving it as a
// second `Class` node named Foo (a DUPLICATE of the real struct). The fix
// classifies impl blocks authoritatively by AST node kind (result.RustImplIdx),
// so an empty/const-only impl is demoted to ImplBlock like any other impl and
// exactly ONE canonical Class node named Foo survives.
//
// Mutation-check: under the pre-fix heuristic this fixture yields THREE Class
// nodes named "Foo" (struct + the two childless impls); the assertions below
// fail. Under the fix there is exactly one.
func TestRustImpl_ConstOnlyAndEmptyImpl_NoDuplicateClass(t *testing.T) {
	src := "" +
		"struct Foo {\n" +
		"    x: i32,\n" +
		"}\n" +
		"\n" +
		"impl Foo {}\n" +
		"\n" +
		"impl Foo {\n" +
		"    const MAX: i32 = 10;\n" +
		"}\n" +
		"\n" +
		"impl Foo {\n" +
		"    fn get(&self) -> i32 { self.x }\n" +
		"}\n"

	res := parseFixture(t, "foo.rs", src)
	if res == nil {
		return // spec not registered (skipped)
	}

	if got := countClassNamed(res, "Foo"); got != 1 {
		t.Fatalf("expected exactly ONE canonical Class node named Foo, got %d "+
			"(a childless impl was misclassified as a duplicate struct)", got)
	}

	// The single canonical Class must own the method from the real impl block,
	// proving the const-only/empty impls did not hijack the canonical slot.
	fooIdx := classIdx1(res, "Foo")
	if fooIdx == 0 {
		t.Fatal("could not resolve the single canonical Foo class index")
	}
	var getParented bool
	for i := range res.Nodes {
		n := &res.Nodes[i]
		if n.Name == "get" && (n.Label == "Method" || n.Label == "Function") {
			if n.ParentID == fooIdx {
				getParented = true
			}
		}
	}
	if !getParented {
		t.Errorf("method get() was not re-parented to the canonical Foo class (idx %d)", fooIdx)
	}
}

// Ordering guard: the empty impl appears BEFORE the struct. Under the old
// heuristic the empty impl was registered into structNodes first (as canonical),
// so it could hijack the canonical slot and later methods would reparent to the
// EMPTY block instead of the real struct. The AST-kind classification is
// order-independent — the struct is always canonical.
func TestRustImpl_EmptyImplBeforeStruct_StructIsCanonical(t *testing.T) {
	src := "" +
		"impl Foo {}\n" +
		"\n" +
		"struct Foo {\n" +
		"    x: i32,\n" +
		"}\n" +
		"\n" +
		"impl Foo {\n" +
		"    fn get(&self) -> i32 { self.x }\n" +
		"}\n"

	res := parseFixture(t, "foo2.rs", src)
	if res == nil {
		return
	}
	if got := countClassNamed(res, "Foo"); got != 1 {
		t.Fatalf("expected exactly ONE canonical Class node named Foo, got %d", got)
	}
}

// Regression guard: a normal struct + a real impl with methods must still fold
// correctly — one Class, methods parented to it, impl demoted to ImplBlock.
func TestRustImpl_NormalStructImpl_StillFolds(t *testing.T) {
	src := "" +
		"struct Bar {\n" +
		"    y: i32,\n" +
		"}\n" +
		"\n" +
		"impl Bar {\n" +
		"    fn area(&self) -> i32 { self.y }\n" +
		"    fn scale(&self, k: i32) -> i32 { self.y * k }\n" +
		"}\n"

	res := parseFixture(t, "bar.rs", src)
	if res == nil {
		return
	}
	if got := countClassNamed(res, "Bar"); got != 1 {
		t.Fatalf("expected exactly ONE Class node named Bar, got %d", got)
	}
	barIdx := classIdx1(res, "Bar")
	methods := 0
	for i := range res.Nodes {
		n := &res.Nodes[i]
		if (n.Name == "area" || n.Name == "scale") && n.ParentID == barIdx {
			methods++
		}
	}
	if methods != 2 {
		t.Errorf("expected 2 methods parented to Bar, got %d", methods)
	}
}
