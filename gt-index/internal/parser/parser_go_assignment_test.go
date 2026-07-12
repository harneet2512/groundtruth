package parser

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// ─────────────────────────────────────────────────────────────────────────────
// Go `expression_list` single-var assignment extraction (a "separate finding" the
// #43 capital→constructor gate was staged for — see TestGoCapitalizedCallNotConstructor
// NOTE at parser.go). tree-sitter-go wraps BOTH sides of `:=`/`=` in `expression_list`
// nodes, so the identifier-LHS extraction (`left.Type()=="identifier"`) never fired and
// Go produced ZERO AssignmentRefs — the entire assignment-based receiver-typing family
// (Strategy 1.96 + the B2 typeflow fixpoint) was DEAD on a Tier-1 language. The fix
// unwraps the single-named-child case; multi-element (tuple unpacking) and augmented
// assignments are ABSTAINED (correct-or-quiet). This is the parser-level RED→GREEN: the
// single-var `:=`/chain assertions FAIL before the unwrap (0 Go assignments) and pass
// after; the tuple/augmented assertions guard the abstains.
// ─────────────────────────────────────────────────────────────────────────────

// parseGoAssignments parses a Go source string and returns its AssignmentRefs.
func parseGoAssignments(t *testing.T, src string) []AssignmentRef {
	t.Helper()
	spec := specs.ForExtension(".go")
	if spec == nil {
		t.Skip("no go spec registered")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "fixture.go")
	if err := os.WriteFile(path, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	sf := walker.SourceFile{Path: path, AbsPath: path, Language: spec.Name, Spec: spec}
	res, err := ParseFile(sf, false)
	if err != nil {
		t.Fatalf("ParseFile: %v", err)
	}
	return res.Assignments
}

// findAssign returns the FIRST assignment for varName in the given scope (or "" scope
// for module level), and whether one was found.
func findAssign(assigns []AssignmentRef, varName string) (AssignmentRef, bool) {
	for _, a := range assigns {
		if a.VarName == varName {
			return a, true
		}
	}
	return AssignmentRef{}, false
}

// TestGoExpressionListSingleVarAssignment — the RED→GREEN. A Go `:=` single-var
// declaration and a chained `t := m.build()` must each record a ViaReturn AssignmentRef
// whose TypeName is the callee (bare for a plain factory, qualified for a chained call),
// exactly the shape BuildAssignmentIndex + the fixpoint consume. Before the unwrap Go
// recorded NOTHING here.
func TestGoExpressionListSingleVarAssignment(t *testing.T) {
	src := "package p\n" +
		"func driver() {\n" +
		"\tm := makeMaker()\n" +
		"\tt := m.build()\n" +
		"\tt.run()\n" +
		"}\n"
	assigns := parseGoAssignments(t, src)

	// m := makeMaker()  → ViaReturn, TypeName = bare callee "makeMaker".
	m, ok := findAssign(assigns, "m")
	if !ok {
		t.Fatalf("Go `m := makeMaker()` recorded NO assignment (expression_list LHS unwrap missing); assigns=%+v", assigns)
	}
	if !m.ViaReturn || m.TypeName != "makeMaker" {
		t.Errorf("m must be ViaReturn TypeName=makeMaker; got ViaReturn=%v TypeName=%q", m.ViaReturn, m.TypeName)
	}
	if m.Scope != "driver" {
		t.Errorf("m assignment must be scoped to the enclosing func `driver`; got scope=%q", m.Scope)
	}

	// t := m.build()  → ViaReturn, TypeName = QUALIFIED callee "m.build" (the chained shape
	// the fixpoint bridges through the prior round's inferred type of m).
	tt, ok := findAssign(assigns, "t")
	if !ok {
		t.Fatalf("Go chained `t := m.build()` recorded NO assignment; assigns=%+v", assigns)
	}
	if !tt.ViaReturn || tt.TypeName != "m.build" {
		t.Errorf("t must be ViaReturn TypeName=m.build (qualified chained receiver); got ViaReturn=%v TypeName=%q", tt.ViaReturn, tt.TypeName)
	}
}

// TestGoPlainAssignExpressionList — a plain `=` (assignment_statement, NOT `:=`) single-var
// is unwrapped and recorded identically. `x = factory()` must record a ViaReturn ref.
func TestGoPlainAssignExpressionList(t *testing.T) {
	src := "package p\n" +
		"var x Thing\n" +
		"func run() {\n" +
		"\tx = factory()\n" +
		"}\n"
	assigns := parseGoAssignments(t, src)
	x, ok := findAssign(assigns, "x")
	if !ok {
		t.Fatalf("Go plain `x = factory()` recorded NO assignment; assigns=%+v", assigns)
	}
	if !x.ViaReturn || x.TypeName != "factory" {
		t.Errorf("x must be ViaReturn TypeName=factory; got ViaReturn=%v TypeName=%q", x.ViaReturn, x.TypeName)
	}
}

// TestGoTupleAssignAbstains — multi-element `a, b := f()` (tuple unpacking) must ABSTAIN:
// the RHS values do not map 1:1 to the LHS names, so a guessed type is worse than none.
// No assignment may be recorded for a or b from the tuple form.
func TestGoTupleAssignAbstains(t *testing.T) {
	src := "package p\n" +
		"func run() {\n" +
		"\ta, b := makeMaker(), makeOther()\n" +
		"\ta.foo()\n" +
		"\tb.bar()\n" +
		"}\n"
	assigns := parseGoAssignments(t, src)
	if a, ok := findAssign(assigns, "a"); ok {
		t.Errorf("tuple unpacking must ABSTAIN; a wrongly recorded %+v", a)
	}
	if b, ok := findAssign(assigns, "b"); ok {
		t.Errorf("tuple unpacking must ABSTAIN; b wrongly recorded %+v", b)
	}
}

// TestGoAugmentedAssignAbstains — an augmented `x += g()` must ABSTAIN: x already holds a
// value of some type and the RHS is NOT its type. No ViaReturn/alias fact may be minted.
func TestGoAugmentedAssignAbstains(t *testing.T) {
	src := "package p\n" +
		"func run() {\n" +
		"\tx := 0\n" +
		"\tx += g()\n" +
		"}\n"
	assigns := parseGoAssignments(t, src)
	// x := 0 records nothing typeful (0 is not a call); the augmented `x += g()` must NOT
	// record a ViaReturn TypeName=g fact.
	for _, a := range assigns {
		if a.VarName == "x" && a.ViaReturn {
			t.Errorf("augmented `x += g()` must ABSTAIN; wrongly recorded ViaReturn %+v", a)
		}
	}
}
