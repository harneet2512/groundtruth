package parser

import (
	"testing"
)

// ─────────────────────────────────────────────────────────────────────────────
// Higher-order callable flow — parser half.
//   - `x = helper` / `x = obj.method` / `x = mod.func` record a ViaSymbol
//     AssignmentRef whose TypeName/TypeQualified carry the RHS symbol.
//   - `self.cb = helper` / `this.f = f` record the field alias verbatim.
//   - every formal parameter emits an IsParameter binding (the value slot the
//     argument→formal flow writes into).
//   - a callsite whose callee is a callable binding and names no defined
//     symbol is marked DispatchForm "function_value".
// ─────────────────────────────────────────────────────────────────────────────

func findAssignment(res *ParseResult, varName string) *AssignmentRef {
	for i := range res.Assignments {
		if res.Assignments[i].VarName == varName {
			return &res.Assignments[i]
		}
	}
	return nil
}

func findCall(res *ParseResult, callee string, line int) *CallRef {
	for i := range res.Calls {
		if res.Calls[i].CalleeName == callee && res.Calls[i].Line == line {
			return &res.Calls[i]
		}
	}
	return nil
}

// Python: `f = helper; f()` — a bare-symbol RHS records a ViaSymbol binding and
// marks the alias call function_value.
func TestCallableAlias_BareSymbol_Python(t *testing.T) {
	src := "def helper():\n" +
		"    pass\n" +
		"def main():\n" +
		"    f = helper\n" +
		"    f()\n"
	res := parseFixture(t, "m.py", src)
	a := findAssignment(res, "f")
	if a == nil {
		t.Fatalf("no assignment recorded for f; assignments=%+v", res.Assignments)
	}
	if !a.ViaSymbol || a.ViaReturn {
		t.Fatalf("f binding = %+v, want ViaSymbol and not ViaReturn", a)
	}
	if a.TypeName != "helper" || a.TypeQualified != "helper" {
		t.Fatalf("f binding names %q/%q, want helper/helper", a.TypeName, a.TypeQualified)
	}
	if a.Scope != "main" {
		t.Fatalf("f binding scope = %q, want main", a.Scope)
	}
	c := findCall(res, "f", 5)
	if c == nil {
		t.Fatalf("f() callsite missing; calls=%+v", res.Calls)
	}
	if c.DispatchForm != "function_value" {
		t.Fatalf("f() DispatchForm = %q, want function_value", c.DispatchForm)
	}
}

// Python: `self.cb = helper` then `self.cb()` — an object-field alias is
// recorded verbatim and marks the field call function_value.
func TestCallableAlias_SelfField_Python(t *testing.T) {
	src := "def helper():\n" +
		"    pass\n" +
		"class C:\n" +
		"    def __init__(self):\n" +
		"        self.cb = helper\n" +
		"    def run(self):\n" +
		"        self.cb()\n"
	res := parseFixture(t, "c.py", src)
	a := findAssignment(res, "self.cb")
	if a == nil {
		t.Fatalf("no assignment recorded for self.cb; assignments=%+v", res.Assignments)
	}
	if !a.ViaSymbol {
		t.Fatalf("self.cb binding = %+v, want ViaSymbol", a)
	}
	if a.TypeName != "helper" {
		t.Fatalf("self.cb binding names %q, want helper", a.TypeName)
	}
	if a.ObjectScope != "C" {
		t.Fatalf("self.cb binding objectScope = %q, want C", a.ObjectScope)
	}
	c := findCall(res, "cb", 7)
	if c == nil {
		t.Fatalf("self.cb() callsite missing; calls=%+v", res.Calls)
	}
	if c.DispatchForm != "function_value" {
		t.Fatalf("self.cb() DispatchForm = %q, want function_value", c.DispatchForm)
	}
	if c.CalleeQualified != "self.cb" {
		t.Fatalf("self.cb() qualified = %q, want self.cb", c.CalleeQualified)
	}
}

// Python: `f = obj.method` — a qualified RHS keeps leaf + qualified text.
func TestCallableAlias_QualifiedSymbol_Python(t *testing.T) {
	src := "def main(obj):\n" +
		"    handler = obj.method\n" +
		"    handler()\n"
	res := parseFixture(t, "m.py", src)
	a := findAssignment(res, "handler")
	if a == nil {
		t.Fatalf("no assignment for handler; assignments=%+v", res.Assignments)
	}
	if !a.ViaSymbol || a.TypeName != "method" || a.TypeQualified != "obj.method" {
		t.Fatalf("handler binding = %+v, want ViaSymbol method/obj.method", a)
	}
	c := findCall(res, "handler", 3)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("handler() = %+v, want function_value", c)
	}
}

// Python: `def wrap(cb): cb()` — the formal emits an IsParameter binding and
// the parameter call is marked function_value.
func TestCallableParam_Python(t *testing.T) {
	src := "def helper():\n" +
		"    pass\n" +
		"def wrap(cb):\n" +
		"    cb()\n" +
		"def main():\n" +
		"    wrap(helper)\n"
	res := parseFixture(t, "w.py", src)
	a := findAssignment(res, "cb")
	if a == nil {
		t.Fatalf("no parameter binding for cb; assignments=%+v", res.Assignments)
	}
	if !a.IsParameter || a.ParameterIndex != 0 {
		t.Fatalf("cb binding = %+v, want IsParameter index 0", a)
	}
	if a.Scope != "wrap" {
		t.Fatalf("cb binding scope = %q, want wrap", a.Scope)
	}
	c := findCall(res, "cb", 4)
	if c == nil {
		t.Fatalf("cb() callsite missing; calls=%+v", res.Calls)
	}
	if c.DispatchForm != "function_value" {
		t.Fatalf("cb() DispatchForm = %q, want function_value", c.DispatchForm)
	}
}

// Python: `def m(self, cb)` — self binds implicitly and consumes NO positional
// index, so cb is ParameterIndex 0 matching `obj.m(arg)`.
func TestCallableParam_SkipsSelf_Python(t *testing.T) {
	src := "class C:\n" +
		"    def m(self, cb):\n" +
		"        cb()\n"
	res := parseFixture(t, "c.py", src)
	a := findAssignment(res, "cb")
	if a == nil {
		t.Fatalf("no parameter binding for cb; assignments=%+v", res.Assignments)
	}
	if !a.IsParameter || a.ParameterIndex != 0 {
		t.Fatalf("cb binding = %+v, want IsParameter index 0 (self skipped)", a)
	}
	if a.Scope != "C.m" || a.ObjectScope != "C" {
		t.Fatalf("cb binding scope/object = %q/%q, want C.m/C", a.Scope, a.ObjectScope)
	}
	if findAssignment(res, "self") != nil {
		t.Fatalf("self must not be recorded as a parameter binding")
	}
}

// Python: `def f(*args, cb)` — a splat ends positional binding; cb is
// keyword-only and must NOT be recorded positionally.
func TestCallableParam_SplatStops_Python(t *testing.T) {
	src := "def f(a, *args, cb):\n" +
		"    cb()\n"
	res := parseFixture(t, "f.py", src)
	if findAssignment(res, "cb") != nil {
		t.Fatalf("keyword-only cb after *args must not be recorded; assignments=%+v", res.Assignments)
	}
	if a := findAssignment(res, "a"); a == nil || !a.IsParameter || a.ParameterIndex != 0 {
		t.Fatalf("a binding = %+v, want IsParameter index 0", a)
	}
}

// Python annotated param: `def use(h: HttpClient)` records the declared type so
// VTA seeds the param with it.
func TestCallableParam_TypedAnnotation_Python(t *testing.T) {
	src := "class HttpClient:\n" +
		"    pass\n" +
		"def use(h: HttpClient):\n" +
		"    h.run()\n"
	res := parseFixture(t, "u.py", src)
	a := findAssignment(res, "h")
	if a == nil || !a.IsParameter || a.TypeName != "HttpClient" {
		t.Fatalf("h binding = %+v, want IsParameter TypeName=HttpClient", a)
	}
}

// Python: `x = compute()` stays a ViaReturn factory binding — the alias path
// must not claim call RHS.
func TestCallableAlias_CallRHSStaysViaReturn_Python(t *testing.T) {
	src := "def main():\n" +
		"    x = compute()\n"
	res := parseFixture(t, "m.py", src)
	a := findAssignment(res, "x")
	if a == nil || !a.ViaReturn || a.ViaSymbol {
		t.Fatalf("x binding = %+v, want ViaReturn and not ViaSymbol", a)
	}
}

// JS: `const f = () => helper(); const g = f; g()` — arrow params, a bare
// identifier RHS through a variable_declarator, and the alias call marking.
func TestCallableAlias_ArrowChain_JS(t *testing.T) {
	src := "function helper() {}\n" +
		"function main() {\n" +
		"  const f = () => helper();\n" +
		"  const g = f;\n" +
		"  g();\n" +
		"}\n"
	res := parseFixture(t, "m.js", src)
	a := findAssignment(res, "g")
	if a == nil {
		t.Fatalf("no assignment for g; assignments=%+v", res.Assignments)
	}
	if !a.ViaSymbol || a.TypeName != "f" {
		t.Fatalf("g binding = %+v, want ViaSymbol f", a)
	}
	c := findCall(res, "g", 5)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("g() = %+v, want function_value", c)
	}
	// The arrow's bare `helper()` call stays static — helper is a defined
	// symbol, not a bound value.
	h := findCall(res, "helper", 3)
	if h == nil || h.DispatchForm != "static" {
		t.Fatalf("helper() inside arrow = %+v, want static", h)
	}
}

// JS/TS: `handler = obj.method; handler()` — member-expression RHS.
func TestCallableAlias_BoundMethod_JS(t *testing.T) {
	src := "function wire(obj) {\n" +
		"  let handler = obj.method;\n" +
		"  handler();\n" +
		"}\n"
	res := parseFixture(t, "w.js", src)
	a := findAssignment(res, "handler")
	if a == nil || !a.ViaSymbol || a.TypeName != "method" || a.TypeQualified != "obj.method" {
		t.Fatalf("handler binding = %+v, want ViaSymbol method/obj.method", a)
	}
	c := findCall(res, "handler", 3)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("handler() = %+v, want function_value", c)
	}
}

// JS: `function wrap(cb) { cb(); }` — function-declaration formals emit
// IsParameter bindings.
func TestCallableParam_FunctionDecl_JS(t *testing.T) {
	src := "function helper() {}\n" +
		"function wrap(cb) {\n" +
		"  cb();\n" +
		"}\n" +
		"function main() {\n" +
		"  wrap(helper);\n" +
		"}\n"
	res := parseFixture(t, "w.js", src)
	a := findAssignment(res, "cb")
	if a == nil || !a.IsParameter || a.ParameterIndex != 0 || a.Scope != "wrap" {
		t.Fatalf("cb binding = %+v, want IsParameter index 0 scope wrap", a)
	}
	c := findCall(res, "cb", 3)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("cb() = %+v, want function_value", c)
	}
}

// TS: arrow `(cb: () => void) => cb()` — a function-typed parameter is a pure
// value slot (no TypeName) and the call still marks function_value.
func TestCallableParam_ArrowTyped_TS(t *testing.T) {
	src := "const helper = () => {};\n" +
		"const wrap = (cb: () => void) => {\n" +
		"  cb();\n" +
		"};\n" +
		"wrap(helper);\n"
	res := parseFixture(t, "w.ts", src)
	a := findAssignment(res, "cb")
	if a == nil || !a.IsParameter {
		t.Fatalf("cb binding = %+v, want IsParameter", a)
	}
	if a.TypeName != "" {
		t.Fatalf("cb function-typed annotation should stay a value slot; TypeName=%q", a.TypeName)
	}
	c := findCall(res, "cb", 3)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("cb() = %+v, want function_value", c)
	}
}

// JS `this.f = f; this.f()` — field alias through `this`.
func TestCallableAlias_ThisField_JS(t *testing.T) {
	src := "function f() {}\n" +
		"class C {\n" +
		"  ctor() { this.f = f; }\n" +
		"  run() { this.f(); }\n" +
		"}\n"
	res := parseFixture(t, "c.js", src)
	a := findAssignment(res, "this.f")
	if a == nil || !a.ViaSymbol || a.TypeName != "f" {
		t.Fatalf("this.f binding = %+v, want ViaSymbol f", a)
	}
	if a.ObjectScope != "C" {
		t.Fatalf("this.f binding objectScope = %q, want C", a.ObjectScope)
	}
	c := findCall(res, "f", 4)
	if c == nil || c.DispatchForm != "function_value" || c.CalleeQualified != "this.f" {
		t.Fatalf("this.f() = %+v, want function_value this.f", c)
	}
}

// JS: `const w = (cb) => cb()` nested inside `outer` — a nested arrow emits no
// node, so the formal stays visible under the enclosing scope ("outer", the
// CallerScope calls inside it carry) while Owner names the nested callable the
// callsite `w(helper)` actually invokes.
func TestCallableParam_NestedArrowOwner_JS(t *testing.T) {
	src := "function helper() {}\n" +
		"function outer() {\n" +
		"  const w = (cb) => {\n" +
		"    cb();\n" +
		"  };\n" +
		"  w(helper);\n" +
		"}\n"
	res := parseFixture(t, "x.js", src)
	a := findAssignment(res, "cb")
	if a == nil || !a.IsParameter || a.ParameterIndex != 0 {
		t.Fatalf("cb binding = %+v, want IsParameter index 0", a)
	}
	if a.Scope != "outer" || a.Owner != "w" {
		t.Fatalf("cb binding scope/owner = %q/%q, want outer/w", a.Scope, a.Owner)
	}
	c := findCall(res, "cb", 4)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("cb() inside nested arrow = %+v, want function_value", c)
	}
}

// Python: `def outer(): def inner(cb): cb(); inner(helper)` — same nested
// split: Scope is the enclosing function, Owner the nested def.
func TestCallableParam_NestedDefOwner_Python(t *testing.T) {
	src := "def helper():\n" +
		"    pass\n" +
		"def outer():\n" +
		"    def inner(cb):\n" +
		"        cb()\n" +
		"    inner(helper)\n"
	res := parseFixture(t, "n.py", src)
	a := findAssignment(res, "cb")
	if a == nil || !a.IsParameter || a.Scope != "outer" || a.Owner != "inner" {
		t.Fatalf("cb binding = %+v, want IsParameter scope=outer owner=inner", a)
	}
	c := findCall(res, "cb", 5)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("cb() inside nested def = %+v, want function_value", c)
	}
}

// An anonymous callback's formals get NO binding — `items.forEach((cb) =>
// cb())` cannot be invoked by name, so recording it would only shadow a
// same-named local.
func TestCallableParam_AnonymousCallbackSkipped_JS(t *testing.T) {
	src := "function outer(items) {\n" +
		"  items.forEach((cb) => { cb(); });\n" +
		"}\n"
	res := parseFixture(t, "x.js", src)
	if a := findAssignment(res, "cb"); a != nil {
		t.Fatalf("anonymous callback formal must not be recorded: %+v", a)
	}
}

// Conservative: `x = "s".join` (literal-rooted receiver) and `x = f()` are NOT
// callable aliases — no ViaSymbol binding is recorded.
func TestCallableAlias_ConservativeSkips_Python(t *testing.T) {
	src := "def main():\n" +
		"    a = \"s\".join\n" +
		"    b = compute()\n" +
		"    c = d[0]\n"
	res := parseFixture(t, "m.py", src)
	if a := findAssignment(res, "a"); a != nil && a.ViaSymbol {
		t.Fatalf("literal-rooted receiver must not be a callable alias: %+v", a)
	}
	if b := findAssignment(res, "b"); b == nil || b.ViaSymbol || !b.ViaReturn {
		t.Fatalf("call RHS must stay ViaReturn: %+v", b)
	}
	if c := findAssignment(res, "c"); c != nil && c.ViaSymbol {
		t.Fatalf("subscript RHS must not be a callable alias: %+v", c)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// HAR-90 item 2 — statically-typed languages: Java method references, Go
// function values, Kotlin callable references.
// ─────────────────────────────────────────────────────────────────────────────

// Java: `Runnable r = this::work` inside a method records a ViaSymbol binding
// whose qualified RHS is the method reference; `r.run()` is marked
// function_value (Java collapses `r.run()` to callee "r" — the SAM method
// name is irrelevant, the alias variable is the lookup key).
func TestCallableAlias_MethodRef_Java(t *testing.T) {
	src := "class Items {\n" +
		"    void handle() {\n" +
		"        Runnable r = this::work;\n" +
		"        r.run();\n" +
		"    }\n" +
		"    void work() {}\n" +
		"}\n"
	res := parseFixture(t, "Items.java", src)
	a := findAssignment(res, "r")
	if a == nil {
		t.Fatalf("no assignment for r; assignments=%+v", res.Assignments)
	}
	if !a.ViaSymbol || a.TypeName != "work" || a.TypeQualified != "this.work" {
		t.Fatalf("r binding = %+v, want ViaSymbol work/this.work", a)
	}
	if a.Scope != "Items.handle" || a.ObjectScope != "Items" {
		t.Fatalf("r binding scope/obj = %q/%q, want Items.handle/Items", a.Scope, a.ObjectScope)
	}
	c := findCall(res, "r", 4)
	if c == nil {
		t.Fatalf("r.run() callsite missing; calls=%+v", res.Calls)
	}
	if c.DispatchForm != "function_value" {
		t.Fatalf("r.run() DispatchForm = %q, want function_value", c.DispatchForm)
	}
}

// Java: a class-level `private Runnable r = this::work;` field declaration is
// a callable alias — the per-function extractor never sees class bodies, so
// the field is harvested separately and binds BOTH the bare name (`r.run()`
// extracts callee "r") and the `this.`-prefixed form (`this.r.run()`).
func TestCallableAlias_FieldDecl_Java(t *testing.T) {
	src := "class Items {\n" +
		"    private Runnable r = this::work;\n" +
		"    void handle() {\n" +
		"        r.run();\n" +
		"        this.r.run();\n" +
		"    }\n" +
		"    void work() {}\n" +
		"}\n"
	res := parseFixture(t, "Items.java", src)
	var bare, field *AssignmentRef
	for i := range res.Assignments {
		a := &res.Assignments[i]
		switch {
		case a.VarName == "r" && a.Scope == "":
			bare = a
		case a.VarName == "this.r":
			field = a
		}
	}
	if bare == nil || !bare.ViaSymbol || bare.ObjectScope != "Items" {
		t.Fatalf("bare field alias = %+v, want ViaSymbol objectScope=Items", bare)
	}
	if field == nil || !field.ViaSymbol || field.ObjectScope != "Items" ||
		field.TypeName != "work" || field.TypeQualified != "this.work" {
		t.Fatalf("this.r field alias = %+v, want ViaSymbol work/this.work obj=Items", field)
	}
	c := findCall(res, "r", 4)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("r.run() = %+v, want function_value", c)
	}
	c2 := findCall(res, "this.r", 5)
	if c2 == nil || c2.DispatchForm != "function_value" {
		t.Fatalf("this.r.run() = %+v, want function_value", c2)
	}
}

// Java: `void register(Runnable cb)` records an IsParameter binding with the
// qualified owner `Items.register`; `cb.run()` marks function_value, and a
// `register(this::work)` callsite captures the method reference as an
// argument name.
func TestCallableParam_Java(t *testing.T) {
	src := "class Items {\n" +
		"    void register(Runnable cb) { cb.run(); }\n" +
		"    void handle() { register(this::work); }\n" +
		"    void work() {}\n" +
		"}\n"
	res := parseFixture(t, "Items.java", src)
	a := findAssignment(res, "cb")
	if a == nil {
		t.Fatalf("no param binding for cb; assignments=%+v", res.Assignments)
	}
	if !a.IsParameter || a.ParameterIndex != 0 || a.Owner != "Items.register" ||
		a.Scope != "Items.register" || a.ObjectScope != "Items" {
		t.Fatalf("cb binding = %+v, want IsParameter idx=0 owner=Items.register obj=Items", a)
	}
	c := findCall(res, "cb", 2)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("cb.run() = %+v, want function_value", c)
	}
	call := findCall(res, "register", 3)
	if call == nil || len(call.ArgumentNames) != 1 || call.ArgumentNames[0] != "this::work" {
		t.Fatalf("register(this::work) args = %+v, want [this::work]", call)
	}
}

// Java: a lambda RHS is anonymous — `Runnable r = () -> {}` records no
// callable alias and `r.run()` is not marked (mirrors the Py/TS rule).
func TestCallableAlias_LambdaSkipped_Java(t *testing.T) {
	src := "class Items {\n" +
		"    void handle() {\n" +
		"        Runnable r = () -> {};\n" +
		"        r.run();\n" +
		"    }\n" +
		"}\n"
	res := parseFixture(t, "Items.java", src)
	for _, a := range res.Assignments {
		if a.VarName == "r" && a.ViaSymbol {
			t.Fatalf("lambda RHS must not be a callable alias: %+v", a)
		}
	}
	if c := findCall(res, "r", 4); c != nil && c.DispatchForm == "function_value" {
		t.Fatalf("r.run() on a lambda binding must stay %q", c.DispatchForm)
	}
}

// Go: `f := helper; f()` — a bare local alias records ViaSymbol and marks the
// call.
func TestCallableAlias_BareLocal_Go(t *testing.T) {
	src := "package main\n\n" +
		"func helper() {}\n\n" +
		"func main() {\n" +
		"    f := helper\n" +
		"    f()\n" +
		"}\n"
	res := parseFixture(t, "m.go", src)
	a := findAssignment(res, "f")
	if a == nil || !a.ViaSymbol || a.TypeName != "helper" || a.TypeQualified != "helper" {
		t.Fatalf("f binding = %+v, want ViaSymbol helper/helper", a)
	}
	if a.Scope != "main" {
		t.Fatalf("f binding scope = %q, want main", a.Scope)
	}
	c := findCall(res, "f", 7)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("f() = %+v, want function_value", c)
	}
}

// Go: `var pkgf = helper` at package level is a module-scope alias (Scope
// "") — visible from every function, so `pkgf()` inside main marks.
func TestCallableAlias_PackageLevel_Go(t *testing.T) {
	src := "package main\n\n" +
		"func helper() {}\n\n" +
		"var pkgf = helper\n\n" +
		"func main() {\n" +
		"    pkgf()\n" +
		"}\n"
	res := parseFixture(t, "m.go", src)
	a := findAssignment(res, "pkgf")
	if a == nil || !a.ViaSymbol || a.Scope != "" {
		t.Fatalf("pkgf binding = %+v, want ViaSymbol scope=\"\"", a)
	}
	c := findCall(res, "pkgf", 8)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("pkgf() = %+v, want function_value", c)
	}
}

// Go: `h := Handler{F: helper}` records the constructor binding (h →
// Handler) AND the keyed function-value field (h.F → helper); `h.F()` marks
// function_value.
func TestCallableAlias_StructField_Go(t *testing.T) {
	src := "package main\n\n" +
		"func helper() string { return \"x\" }\n\n" +
		"type Handler struct { F func() string }\n\n" +
		"func main() {\n" +
		"    h := Handler{F: helper}\n" +
		"    h.F()\n" +
		"}\n"
	res := parseFixture(t, "m.go", src)
	h := findAssignment(res, "h")
	if h == nil || h.TypeName != "Handler" || h.ViaSymbol {
		t.Fatalf("h binding = %+v, want constructor Handler (not ViaSymbol)", h)
	}
	f := findAssignment(res, "h.F")
	if f == nil || !f.ViaSymbol || f.TypeName != "helper" || f.Scope != "main" {
		t.Fatalf("h.F binding = %+v, want ViaSymbol helper scope=main", f)
	}
	c := findCall(res, "F", 9)
	if c == nil || c.DispatchForm != "function_value" || c.CalleeQualified != "h.F" {
		t.Fatalf("h.F() = %+v, want function_value qualified h.F", c)
	}
}

// Go: `func wrap(cb func() string) { cb() }` + `wrap(helper)` — the
// func-typed formal is a pure callable value slot (IsParameter, no TypeName)
// and the callsite records the bare-identifier argument.
func TestCallableParam_Go(t *testing.T) {
	src := "package main\n\n" +
		"func helper() string { return \"x\" }\n\n" +
		"func wrap(cb func() string) { cb() }\n\n" +
		"func main() {\n" +
		"    wrap(helper)\n" +
		"}\n"
	res := parseFixture(t, "m.go", src)
	a := findAssignment(res, "cb")
	if a == nil || !a.IsParameter || a.ParameterIndex != 0 ||
		a.TypeName != "" || a.Owner != "wrap" || a.Scope != "wrap" {
		t.Fatalf("cb binding = %+v, want IsParameter idx=0 owner=wrap", a)
	}
	c := findCall(res, "cb", 5)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("cb() = %+v, want function_value", c)
	}
	call := findCall(res, "wrap", 8)
	if call == nil || len(call.ArgumentNames) != 1 || call.ArgumentNames[0] != "helper" {
		t.Fatalf("wrap(helper) args = %+v, want [helper]", call)
	}
}

// Go conservative cases: multi-element destructuring records no binding;
// `x := compute()` stays ViaReturn; a rebind `f = other` records a second
// (disagreeing) write so the resolver must abstain.
func TestCallableAlias_Conservative_Go(t *testing.T) {
	src := "package main\n\n" +
		"func helper() {}\n" +
		"func other() {}\n" +
		"func compute() int { return 1 }\n\n" +
		"func main() {\n" +
		"    x := compute()\n" +
		"    a, b := helper, other\n" +
		"    f := helper\n" +
		"    f = other\n" +
		"    f()\n" +
		"}\n"
	res := parseFixture(t, "m.go", src)
	if a := findAssignment(res, "a"); a != nil && a.ViaSymbol {
		t.Fatalf("tuple destructure must not record a: %+v", a)
	}
	if x := findAssignment(res, "x"); x == nil || x.ViaSymbol || !x.ViaReturn {
		t.Fatalf("x binding = %+v, want ViaReturn", x)
	}
	// Two disagreeing writes: f→helper and f→other both recorded; the
	// resolver's agreement rule abstains (tested at resolver level), but the
	// callsite still marks — a binding EXISTS, it is just ambiguous.
	var writes int
	for i := range res.Assignments {
		if res.Assignments[i].VarName == "f" && res.Assignments[i].ViaSymbol {
			writes++
		}
	}
	if writes != 2 {
		t.Fatalf("expected 2 ViaSymbol writes for f, got %d (%+v)", writes, res.Assignments)
	}
}

// Kotlin: `val f = ::helper; f()` — a callable_reference RHS records a
// ViaSymbol binding (qualified text normalized to ".helper") and marks the
// call.
func TestCallableAlias_CallableRef_Kotlin(t *testing.T) {
	src := "fun helper(): String = \"x\"\n\n" +
		"fun main() {\n" +
		"    val f = ::helper\n" +
		"    f()\n" +
		"}\n"
	res := parseFixture(t, "m.kt", src)
	a := findAssignment(res, "f")
	if a == nil || !a.ViaSymbol || a.TypeName != "helper" {
		t.Fatalf("f binding = %+v, want ViaSymbol helper", a)
	}
	if a.Scope != "main" {
		t.Fatalf("f binding scope = %q, want main", a.Scope)
	}
	c := findCall(res, "f", 5)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("f() = %+v, want function_value", c)
	}
}

// Kotlin: `fun wrap(cb: () -> String) { cb() }` + `wrap(::helper)` — the
// function-typed formal is an IsParameter slot and the callable_reference
// argument is captured.
func TestCallableParam_Kotlin(t *testing.T) {
	src := "fun helper(): String = \"x\"\n\n" +
		"fun wrap(cb: () -> String) { cb() }\n\n" +
		"fun main() {\n" +
		"    wrap(::helper)\n" +
		"}\n"
	res := parseFixture(t, "m.kt", src)
	a := findAssignment(res, "cb")
	if a == nil || !a.IsParameter || a.ParameterIndex != 0 || a.Owner != "wrap" {
		t.Fatalf("cb binding = %+v, want IsParameter idx=0 owner=wrap", a)
	}
	c := findCall(res, "cb", 3)
	if c == nil || c.DispatchForm != "function_value" {
		t.Fatalf("cb() = %+v, want function_value", c)
	}
	call := findCall(res, "wrap", 6)
	if call == nil || len(call.ArgumentNames) != 1 || call.ArgumentNames[0] != "::helper" {
		t.Fatalf("wrap(::helper) args = %+v, want [::helper]", call)
	}
}

// Kotlin conservative: `val f = { "x" }` — a lambda RHS records no callable
// alias.
func TestCallableAlias_LambdaSkipped_Kotlin(t *testing.T) {
	src := "fun main() {\n" +
		"    val f = { \"x\" }\n" +
		"    f()\n" +
		"}\n"
	res := parseFixture(t, "m.kt", src)
	for _, a := range res.Assignments {
		if a.VarName == "f" && a.ViaSymbol {
			t.Fatalf("lambda RHS must not be a callable alias: %+v", a)
		}
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// ArgumentTexts (v15.4): parser-exact top-level argument texts per callsite —
// the substrate consumers use instead of re-splitting call text.
// ─────────────────────────────────────────────────────────────────────────────

// Keyword args, spreads, and nested calls all survive verbatim, in order.
func TestArgumentTexts_KwargsAndSpreads_Python(t *testing.T) {
	src := "def main():\n" +
		"    f(1, x=2, *seq, g(a, b), **opts)\n"
	res := parseFixture(t, "m.py", src)
	call := findCall(res, "f", 2)
	if call == nil {
		t.Fatalf("f() callsite missing; calls=%+v", res.Calls)
	}
	want := []string{"1", "x=2", "*seq", "g(a, b)", "**opts"}
	if len(call.ArgumentTexts) != len(want) {
		t.Fatalf("ArgumentTexts = %v, want %v", call.ArgumentTexts, want)
	}
	for i, w := range want {
		if call.ArgumentTexts[i] != w {
			t.Fatalf("ArgumentTexts[%d] = %q, want %q (all=%v)",
				i, call.ArgumentTexts[i], w, call.ArgumentTexts)
		}
	}
}

// Multi-call line: each callsite carries only its own arguments.
func TestArgumentTexts_PerCallsiteOnSharedLine_Go(t *testing.T) {
	src := "package main\n" +
		"func line() int {\n" +
		"	return helper(1) + cb(arg1, arg2)\n" +
		"}\n"
	res := parseFixture(t, "m.go", src)
	h := findCall(res, "helper", 3)
	c := findCall(res, "cb", 3)
	if h == nil || c == nil {
		t.Fatalf("calls=%+v, want helper@3 and cb@3", res.Calls)
	}
	if len(h.ArgumentTexts) != 1 || h.ArgumentTexts[0] != "1" {
		t.Fatalf("helper ArgumentTexts = %v, want [1]", h.ArgumentTexts)
	}
	if len(c.ArgumentTexts) != 2 || c.ArgumentTexts[0] != "arg1" || c.ArgumentTexts[1] != "arg2" {
		t.Fatalf("cb ArgumentTexts = %v, want [arg1 arg2]", c.ArgumentTexts)
	}
}

// Empty arg list: ArgumentTexts stays nil/empty — never fabricated.
func TestArgumentTexts_EmptyCall_TS(t *testing.T) {
	src := "function main(): void {\n" +
		"	f();\n" +
		"}\n"
	res := parseFixture(t, "m.ts", src)
	call := findCall(res, "f", 2)
	if call == nil {
		t.Fatalf("f() callsite missing; calls=%+v", res.Calls)
	}
	if len(call.ArgumentTexts) != 0 {
		t.Fatalf("ArgumentTexts = %v, want empty", call.ArgumentTexts)
	}
}
