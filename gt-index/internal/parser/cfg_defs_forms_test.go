package parser

// cfg_defs_forms_test.go — every declaration/binding form of the CFG
// languages must produce exactly its bound names as defs, and the READ side
// of a declaration (initializers, return values, RHS expression lists) must
// never be recorded as a def. HAR-90 F15: Go `:=` produced no def rows
// because bindPattern had no expression_list case, while collectDefsInto's
// generic expression_list case bound every RHS/return identifier as a def.

import (
	"fmt"
	"testing"
)

func defLines(fn CFGFunc) map[string]bool {
	out := map[string]bool{}
	for _, d := range fn.Defs {
		out[fmt.Sprintf("%s@%d", d.VarName, d.Line)] = true
	}
	return out
}

func assertDefForms(t *testing.T, fn CFGFunc, want, notWant []string) {
	t.Helper()
	got := defLines(fn)
	for _, w := range want {
		if !got[w] {
			t.Errorf("missing def %s; defs=%v", w, got)
		}
	}
	for _, nw := range notWant {
		if got[nw] {
			t.Errorf("spurious def %s (a read recorded as a definition); defs=%v", nw, got)
		}
	}
}

// TestCFGGoShortVarDeclOnlyDefs: variables defined ONLY by `:=` (no later
// `=` that would mask the miss) must still produce def rows.
func TestCFGGoShortVarDeclOnlyDefs(t *testing.T) {
	fn, _ := parseCFG(t, ".go", "f", `package p
func f(a, b int) int {
	y := a + b
	z := helper(y)
	w, err := two()
	return y + z + w + err
}`)
	assertDefForms(t, fn,
		[]string{"y@3", "z@4", "w@5", "err@5"},
		[]string{"a@3", "b@3", "helper@4", "y@4", "y@6", "z@6", "w@6", "err@6"})
}

func TestCFGGoDeclarationForms(t *testing.T) {
	fn, _ := parseCFG(t, ".go", "f", `package p
func f(m map[string]int, xs []int, ch chan int, x interface{}) int {
	var mm, nn int = s1, t1
	var k = kv
	p, q = r, s
	for i, v := range xs { use(i, v) }
	for j = range xs {}
	if vv, ok := m["k"]; ok { use(vv) }
	switch sw := g(); sw { case 1: }
	switch tv := x.(type) { case int: use(tv) }
	select { case cv, cok := <-ch: use(cv, cok) }
	select { case one := <-ch: use(one) }
	return mm, nn
}`)
	assertDefForms(t, fn,
		[]string{"mm@3", "nn@3", "k@4", "p@5", "q@5", "i@6", "v@6", "j@7",
			"vv@8", "ok@8", "sw@9", "tv@10", "cv@11", "cok@11", "one@12"},
		[]string{"s1@3", "t1@3", "kv@4", "r@5", "s@5", "xs@6", "mm@13", "nn@13"})
}

func TestCFGJSDestructuringDefs(t *testing.T) {
	fn, _ := parseCFG(t, ".js", "f", `function f(obj, arr, m, xs, o) {
	const {a, b: c, d = 1, ...rest} = obj;
	let [x, , y = 2, ...zs] = arr;
	for (const [k, v] of m) { use(k, v) }
	for (const {p} of xs) { use(p) }
	try { g() } catch ({message}) { use(message) }
	({a2, b2} = o);
	[m1, m2] = [m2, m1];
	return a + c;
}`)
	assertDefForms(t, fn,
		[]string{"a@2", "c@2", "d@2", "rest@2", "x@3", "y@3", "zs@3",
			"k@4", "v@4", "p@5", "message@6", "a2@7", "b2@7", "m1@8", "m2@8"},
		[]string{"obj@2", "b@2", "arr@3", "a@9", "c@9"})
}

func TestCFGTSTypedDestructuringDefs(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "f", `function f(obj: any): number {
	const t: number = 1;
	const {a, b}: {a: number, b: number} = obj;
	let [u, w] = pair(obj);
	return t + a;
}`)
	assertDefForms(t, fn, []string{"t@2", "a@3", "b@3", "u@4", "w@4"},
		[]string{"obj@3", "pair@4"})
}

func TestCFGJavaLocalDeclarationForms(t *testing.T) {
	fn, _ := parseCFG(t, ".java", "f", `class C { int f(java.util.List<String> list, Object o) {
	int a = 1, b = a2;
	var x = g();
	for (String s : list) { use(s); }
	try (var r = open(); InputStream s2 = src) { use(r); }
	if (o instanceof Foo foo) { use(foo); }
	return a + b;
} }`)
	assertDefForms(t, fn,
		[]string{"a@2", "b@2", "x@3", "s@4", "r@5", "s2@5", "foo@6"},
		[]string{"a2@2", "g@3", "src@5", "a@7", "b@7"})
}

// TestCFGGoSwitchInitializerReads: the Go switch initializer is evaluated at
// the dispatch head, so its reads must be anchored like the if initializer's.
func TestCFGGoSwitchInitializerReads(t *testing.T) {
	fn, _ := parseCFG(t, ".go", "f", `package p
func f() int {
	switch sw := g(seed); sw { case 1: }
	return 0
}`)
	got := map[string]bool{}
	for _, u := range fn.Uses {
		got[fmt.Sprintf("%s@%d", u.VarName, u.Line)] = true
	}
	for _, w := range []string{"g@3", "seed@3", "sw@3"} {
		if !got[w] {
			t.Errorf("missing use %s; uses=%v", w, got)
		}
	}
}
