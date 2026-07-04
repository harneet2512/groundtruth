package parser

// ─────────────────────────────────────────────────────────────────────────────
// Stage 5 (ORACLE_ARCHITECTURE_PLAN.md) — contract completeness:
// extractSignature must capture the FULL declaration header up to the body
// opener — trait bounds, where-clauses, generic constraints, throws clauses,
// multi-line parameter lists — for EVERY language with a body field (LEG 1).
//
// The boa finding (run 27307362054, [86]): the old first-line truncation
// stripped `T: Trace + 'static` from
// `from_copy_closure_with_captures`'s where-clause, so the [CALLEE] renderer
// (which renders nodes.signature verbatim) could not carry the killing fact
// (LEG 2 fixture: TestSignature_Rust_BoaShape_TraceBound).
//
// RED→GREEN: all multi-line-header tests fail on the first-line slice and
// pass once the header is captured to body-open. Single-line tests pin the
// unchanged behavior; the trait-signature-item test pins the legacy fallback.
// ─────────────────────────────────────────────────────────────────────────────

import (
	"strings"
	"testing"
)

func sigOf(t *testing.T, res *ParseResult, name string) string {
	t.Helper()
	for _, n := range res.Nodes {
		if n.Name == name && (n.Label == "Function" || n.Label == "Method") {
			return n.Signature
		}
	}
	names := make([]string, 0, len(res.Nodes))
	for _, n := range res.Nodes {
		names = append(names, n.Label+":"+n.Name)
	}
	t.Fatalf("node %q not found; have: %v", name, names)
	return ""
}

func wantInSig(t *testing.T, sig string, wants ...string) {
	t.Helper()
	for _, w := range wants {
		if !strings.Contains(sig, w) {
			t.Errorf("signature %q missing %q", sig, w)
		}
	}
}

func wantNotInSig(t *testing.T, sig string, nots ...string) {
	t.Helper()
	for _, w := range nots {
		if strings.Contains(sig, w) {
			t.Errorf("signature %q must not contain body fragment %q", sig, w)
		}
	}
}

// ── Rust ────────────────────────────────────────────────────────────────────

// The where-clause is part of the declaration header and must survive.
func TestSignature_Rust_WhereClause(t *testing.T) {
	src := "fn foo<T: Trace + 'static>(x: T) -> i32\n" +
		"where\n" +
		"    T: Send,\n" +
		"{\n" +
		"    body_marker()\n" +
		"}\n"
	res := parseFixture(t, "lib.rs", src)
	sig := sigOf(t, res, "foo")
	wantInSig(t, sig, "T: Trace + 'static", "where", "T: Send")
	wantNotInSig(t, sig, "body_marker", "{")
}

// LEG 2 fixture — the boa [86] shape: nodes.signature must carry the killing
// fact `T: Trace + 'static` so [CALLEE] can render it.
func TestSignature_Rust_BoaShape_TraceBound(t *testing.T) {
	src := "fn from_copy_closure_with_captures<F, T>(closure: F, captures: T) -> NativeFunction\n" +
		"where\n" +
		"    F: Fn(&JsValue, &[JsValue], &mut T, &mut Context) -> JsResult<JsValue> + Copy + 'static,\n" +
		"    T: Trace + 'static,\n" +
		"{\n" +
		"    body_marker()\n" +
		"}\n"
	res := parseFixture(t, "native_function.rs", src)
	sig := sigOf(t, res, "from_copy_closure_with_captures")
	wantInSig(t, sig, "T: Trace + 'static", "-> NativeFunction", "Copy + 'static")
	wantNotInSig(t, sig, "body_marker")
}

// A trait's bodyless function_signature_item has no body child → the legacy
// first-line behavior must be preserved exactly (no whole-node swallowing).
func TestSignature_Rust_TraitSignatureItem_LegacyFallback(t *testing.T) {
	src := "trait T1 {\n" +
		"    fn sig_only(x: i32) -> i32;\n" +
		"}\n"
	res := parseFixture(t, "t.rs", src)
	sig := sigOf(t, res, "sig_only")
	if sig != "fn sig_only(x: i32) -> i32;" {
		t.Errorf("trait signature item = %q, want legacy first-line", sig)
	}
}

// ── Go ──────────────────────────────────────────────────────────────────────

// Go 1.18+ generic constraint lives in the header brackets.
func TestSignature_Go_GenericConstraint(t *testing.T) {
	src := "package p\n\n" +
		"func Foo[T constraints.Integer](x T) error {\n" +
		"\treturn nil\n" +
		"}\n"
	res := parseFixture(t, "p.go", src)
	sig := sigOf(t, res, "Foo")
	wantInSig(t, sig, "[T constraints.Integer]", "(x T) error")
	wantNotInSig(t, sig, "return")
}

// Multi-line parameter lists join onto one line; the receiver must stay at the
// signature head so goReceiverType (which parents methods to their struct)
// keeps working on the new shape.
func TestSignature_Go_MultiLineParams_ReceiverIntact(t *testing.T) {
	src := "package p\n\n" +
		"type Recv struct{}\n\n" +
		"func (r *Recv) Bar(\n" +
		"\ta int,\n" +
		"\tb string,\n" +
		") (int, error) {\n" +
		"\treturn 0, \"\"\n" +
		"}\n"
	res := parseFixture(t, "p.go", src)
	sig := sigOf(t, res, "Bar")
	wantInSig(t, sig, "b string", "(int, error)")
	if !strings.HasPrefix(sig, "func (r *Recv) Bar(") {
		t.Errorf("signature %q must start with the receiver header", sig)
	}
	wantNotInSig(t, sig, "return")
	if got := goReceiverType(sig); got != "Recv" {
		t.Errorf("goReceiverType(%q) = %q, want Recv (method parenting consumes this)", sig, got)
	}
	for _, n := range res.Nodes {
		if n.Name == "Bar" && n.Label != "Method" {
			t.Errorf("Bar label = %q, want Method (receiver parenting regressed)", n.Label)
		}
	}
}

// ── Python ──────────────────────────────────────────────────────────────────

// Single-line defs are already complete — pin the unchanged output (incl. ':').
func TestSignature_Python_SingleLine_Unchanged(t *testing.T) {
	src := "def foo(x: int) -> Optional[str]:\n    return None\n"
	res := parseFixture(t, "m.py", src)
	sig := sigOf(t, res, "foo")
	if sig != "def foo(x: int) -> Optional[str]:" {
		t.Errorf("signature = %q, want unchanged single-line def", sig)
	}
}

// Decorators live on the wrapping decorated_definition node, not the
// function_definition — they must NOT be swallowed into the signature.
func TestSignature_Python_DecoratorNotSwallowed(t *testing.T) {
	src := "@decorator\ndef bar(y):\n    return y\n"
	res := parseFixture(t, "m.py", src)
	sig := sigOf(t, res, "bar")
	if sig != "def bar(y):" {
		t.Errorf("signature = %q, want %q", sig, "def bar(y):")
	}
	wantNotInSig(t, sig, "@decorator")
}

// Multi-line def headers join onto one line, keeping defaults + return type.
func TestSignature_Python_MultiLineDef(t *testing.T) {
	src := "def baz(\n" +
		"    a: int,\n" +
		"    b: str = \"x\",\n" +
		") -> bool:\n" +
		"    return True\n"
	res := parseFixture(t, "m.py", src)
	sig := sigOf(t, res, "baz")
	wantInSig(t, sig, "b: str = \"x\"", "-> bool:")
	wantNotInSig(t, sig, "return True")
}

// ── TypeScript ──────────────────────────────────────────────────────────────

// The `extends` constraint and return type are header facts.
func TestSignature_TypeScript_ExtendsConstraint(t *testing.T) {
	src := "function foo<T extends Base>(\n" +
		"  x: T,\n" +
		"): Promise<T> {\n" +
		"  return bodyMarker(x);\n" +
		"}\n"
	res := parseFixture(t, "f.ts", src)
	sig := sigOf(t, res, "foo")
	wantInSig(t, sig, "T extends Base", ": Promise<T>")
	wantNotInSig(t, sig, "bodyMarker")
}

// ── Java ────────────────────────────────────────────────────────────────────

// throws clause = Java's constraint; type-parameter bounds precede the return
// type. Both are header facts that the first-line slice dropped.
func TestSignature_Java_ThrowsAndBounds(t *testing.T) {
	src := "class Sorter {\n" +
		"    public <T extends Comparable<T>> void sort(List<T> list)\n" +
		"            throws IOException {\n" +
		"        helper();\n" +
		"    }\n" +
		"}\n"
	res := parseFixture(t, "Sorter.java", src)
	sig := sigOf(t, res, "sort")
	wantInSig(t, sig, "<T extends Comparable<T>>", "throws IOException")
	wantNotInSig(t, sig, "helper")
}

// ── Clip rails (language-agnostic) ──────────────────────────────────────────

// The pre-existing per-line 200-char clip is kept: a single pathological
// header line is still clipped at 200.
func TestSignature_PerLine200Clip_Kept(t *testing.T) {
	long := strings.Repeat("a", 300)
	src := "def f(x_" + long + "):\n    pass\n"
	res := parseFixture(t, "m.py", src)
	sig := sigOf(t, res, "f")
	if len(sig) > 200 {
		t.Errorf("single-line signature len = %d, want <= 200 (per-line clip)", len(sig))
	}
}

// A pathological many-line header is bounded by the total cap.
func TestSignature_TotalCap_Bounded(t *testing.T) {
	var b strings.Builder
	b.WriteString("def g(\n")
	for i := 0; i < 60; i++ {
		b.WriteString("    parameter_name_number_xyz")
		b.WriteString(strings.Repeat("z", 10))
		b.WriteString(",\n")
	}
	b.WriteString("):\n    pass\n")
	res := parseFixture(t, "m.py", b.String())
	sig := sigOf(t, res, "g")
	if len(sig) > 1000 {
		t.Errorf("signature len = %d, want <= 1000 (total cap)", len(sig))
	}
}
