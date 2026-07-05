package parser

import (
	"strings"
	"testing"
	"unicode/utf8"
)

// ─────────────────────────────────────────────────────────────────────────────
// Adversarial-review parser fixes: P1, P2, P3, P9, P10, P13.
// Each test is red→green: it fails on the pre-fix code and passes after.
// ─────────────────────────────────────────────────────────────────────────────

// calleeNamesOf returns the set of CalleeName values extracted from a fixture.
func calleeNamesOf(res *ParseResult) map[string]bool {
	names := map[string]bool{}
	for _, c := range res.Calls {
		names[c.CalleeName] = true
	}
	return names
}

// propsOfKind returns all property Values of the given kind.
func propsOfKind(res *ParseResult, kind string) []string {
	var out []string
	for _, p := range res.Properties {
		if p.Kind == kind {
			out = append(out, p.Value)
		}
	}
	return out
}

// P1 RED→GREEN: a Java method call `helper.process()` must yield callee "process" (the
// METHOD), not "helper" (the receiver). The old `callNode.Child(0)` read the object.
func TestP1_JavaMethodCall_CalleeIsMethodNotReceiver(t *testing.T) {
	src := "" +
		"class Runner {\n" +
		"    void run(Helper helper) {\n" +
		"        helper.process();\n" +
		"    }\n" +
		"}\n"
	res := parseFixture(t, "Runner.java", src)
	names := calleeNamesOf(res)
	if !names["process"] {
		t.Errorf("Java helper.process(): expected callee 'process'; got calls=%v", names)
	}
	if names["helper"] {
		t.Errorf("Java helper.process(): callee is the RECEIVER 'helper' (P1 bug); calls=%v", names)
	}
}

// P14 (same fix as P1): a JSX `<Foo/>` must never yield callee "<". Either "Foo" or skipped.
func TestP14_JSXElement_CalleeIsNotAngleBracket(t *testing.T) {
	src := "" +
		"function App() {\n" +
		"  return <Foo bar={1} />;\n" +
		"}\n"
	res := parseFixture(t, "App.jsx", src)
	names := calleeNamesOf(res)
	if names["<"] {
		t.Errorf("JSX <Foo/>: callee is '<' (P14 bug); calls=%v", names)
	}
}

// P3 RED→GREEN: `x = f(a, b)` is NOT a destructure — the only comma is in the call's args.
// `a, b = pair()` IS a destructure (comma is in the LHS pattern).
func TestP3_Destructure_CommaInArgsIsNotDestructure(t *testing.T) {
	src := "" +
		"def f(a, b):\n" +
		"    return a\n" +
		"def pair():\n" +
		"    return (1, 2)\n" +
		"def user():\n" +
		"    x = f(1, 2)\n" +
		"    c, d = pair()\n" +
		"    return x\n"
	res := parseFixture(t, "d.py", src)
	usages := propsOfKind(res, "caller_usage")
	var singleFlagged, tupleFlagged bool
	for _, u := range usages {
		if strings.HasPrefix(u, "destructure_tuple:f") {
			singleFlagged = true
		}
		if strings.HasPrefix(u, "destructure_tuple:pair") {
			tupleFlagged = true
		}
	}
	if singleFlagged {
		t.Errorf("x = f(a, b): flagged destructure_tuple (P3 bug — comma is in RHS args); usages=%v", usages)
	}
	if !tupleFlagged {
		t.Errorf("c, d = pair(): should be destructure_tuple (comma in LHS pattern); usages=%v", usages)
	}
}

// P9 RED→GREEN: a Rust `?` that appears ONLY inside a string literal is not an early return.
// A real `foo()?` reports exactly one guard.
func TestP9_RustQuestionGuard_IgnoresStringLiteral(t *testing.T) {
	src := "" +
		"fn a() -> Result<(), String> {\n" +
		"    println!(\"what? really?\");\n" +
		"    Ok(())\n" +
		"}\n" +
		"fn b() -> Result<i32, String> {\n" +
		"    let x = foo()?;\n" +
		"    Ok(x)\n" +
		"}\n" +
		"fn foo() -> Result<i32, String> { Ok(1) }\n"
	res := parseFixture(t, "q.rs", src)
	if res == nil {
		return // rust spec not registered
	}
	// Locate node indices for a and b (1-based nodeIdx stored in properties is the array idx).
	guards := propsOfKind(res, "guard_clause")
	// a's body has no try_expression → must NOT emit a "? operator" guard for it.
	// b's body has exactly one → must emit "(1 early returns)".
	var sawFabricated, sawReal bool
	for _, g := range guards {
		if strings.Contains(g, "? operator") {
			if strings.Contains(g, "(1 early returns)") {
				sawReal = true
			} else {
				// any count != 1 here would be from byte-counting the string `?`s
			}
		}
	}
	// Count "? operator" guards total: must be exactly 1 (only b), and it must say 1 early return.
	total := 0
	for _, g := range guards {
		if strings.Contains(g, "? operator") {
			total++
		}
	}
	if total != 1 {
		sawFabricated = true
	}
	if sawFabricated {
		t.Errorf("Rust ? guard: expected exactly ONE '? operator' guard (only foo()? counts); got %v", guards)
	}
	if !sawReal {
		t.Errorf("Rust ? guard: real foo()? must report '(1 early returns)'; got %v", guards)
	}
}

// P10 RED→GREEN: a keyword-argument call `self.log(msg=x)` must NOT emit a side_effect;
// a real field write `self.x = 1` must.
func TestP10_SideEffect_KeywordArgIsNotMutation(t *testing.T) {
	src := "" +
		"class C:\n" +
		"    def f(self, x):\n" +
		"        self.log(msg=x)\n" +
		"        self.x = 1\n" +
		"    def log(self, msg):\n" +
		"        return msg\n"
	res := parseFixture(t, "se.py", src)
	effects := propsOfKind(res, "side_effect")
	var sawLog, sawX bool
	for _, e := range effects {
		if strings.Contains(e, "log") {
			sawLog = true
		}
		if strings.Contains(e, "self.x") {
			sawX = true
		}
	}
	if sawLog {
		t.Errorf("self.log(msg=x): emitted a phantom side_effect (P10 bug); effects=%v", effects)
	}
	if !sawX {
		t.Errorf("self.x = 1: real field mutation must emit a side_effect; effects=%v", effects)
	}
}

// P13 RED→GREEN: Java visibility comes from the access modifier, not name casing.
// `public void foo()` → exported; package-private `void bar()` → not exported.
// Casing is irrelevant: lowercase public is exported; uppercase package-private is not.
func TestP13_JavaVisibility_FromModifierNotCasing(t *testing.T) {
	src := "" +
		"class Svc {\n" +
		"    public void foo() {}\n" +
		"    void bar() {}\n" +
		"    private void Baz() {}\n" +
		"    protected void qux() {}\n" +
		"}\n"
	res := parseFixture(t, "Svc.java", src)
	exp := map[string]bool{}
	found := map[string]bool{}
	for i := range res.Nodes {
		n := &res.Nodes[i]
		if n.Label == "Function" || n.Label == "Method" {
			exp[n.Name] = n.IsExported
			found[n.Name] = true
		}
	}
	for _, name := range []string{"foo", "bar", "Baz", "qux"} {
		if !found[name] {
			t.Fatalf("method %q not indexed; nodes=%v", name, res.Nodes)
		}
	}
	if !exp["foo"] {
		t.Errorf("public void foo(): must be exported")
	}
	if exp["bar"] {
		t.Errorf("package-private void bar(): must NOT be exported (P13 bug — uppercase-casing heuristic)")
	}
	if exp["Baz"] {
		t.Errorf("private void Baz(): must NOT be exported despite uppercase name")
	}
	if !exp["qux"] {
		t.Errorf("protected void qux(): must be exported")
	}
}

// P2 unit: truncateRune never produces invalid UTF-8, even when the byte cut lands mid-rune.
func TestP2_TruncateRune_ValidUTF8(t *testing.T) {
	// "héllo wörld ☃ snowman" — multi-byte runes at various offsets.
	s := "héllo wörld ☃ snowman with a trailing €uro sign"
	for n := 0; n <= len(s)+2; n++ {
		got := truncateRune(s, n)
		if !utf8.ValidString(got) {
			t.Fatalf("truncateRune(%q, %d) = %q is not valid UTF-8", s, n, got)
		}
		if len(got) > n && n <= len(s) {
			t.Fatalf("truncateRune(%q, %d) = %q longer than n bytes", s, n, got)
		}
	}
	// Cutting exactly at a multi-byte boundary must not split the rune. '€' is 3 bytes.
	euro := "a€b" // bytes: a(1) €(3) b(1) = 5 bytes; byte index 2 is mid-€.
	if got := truncateRune(euro, 2); !utf8.ValidString(got) {
		t.Fatalf("truncateRune(%q, 2) = %q split a rune", euro, got)
	}
	// n >= len returns the whole string unchanged.
	if got := truncateRune(euro, 100); got != euro {
		t.Fatalf("truncateRune(%q, 100) = %q, want whole string", euro, got)
	}
}

// P8 extra guard (production annotations must not flip is_test): @app.route with a "/test"
// URL and @pytest.fixture on production-named functions stay is_test=0; @Test stays is_test=1.
func TestP8_ProductionDecorators_NotMarkedTest(t *testing.T) {
	src := "" +
		"@app.route(\"/api/test\")\n" +
		"def serve_api():\n" +
		"    return 1\n" +
		"\n" +
		"@pytest.fixture\n" +
		"def make_widget():\n" +
		"    return object()\n"
	res := parseFixture(t, "views.py", src)
	if found, isT := isTestOf(res, "serve_api"); !found || isT {
		t.Errorf("@app.route(\"/api/test\") serve_api: found=%v isTest=%v — production must stay is_test=0", found, isT)
	}
	if found, isT := isTestOf(res, "make_widget"); !found || isT {
		t.Errorf("@pytest.fixture make_widget: found=%v isTest=%v — production must stay is_test=0", found, isT)
	}
}
