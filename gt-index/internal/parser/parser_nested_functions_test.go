package parser

import "testing"

// B1-#5: walkNode recurses into function bodies so NESTED function definitions become
// nodes, and extractCalls stops at NAMED nested-function boundaries so a nested function's
// calls attribute to IT (not the enclosing function), while ANONYMOUS nested functions are
// traversed through so their calls still attribute to the nearest named ancestor.

func nestedNodeIdx(res *ParseResult, name string) int {
	for i, n := range res.Nodes {
		if n.Name == name {
			return i
		}
	}
	return -1
}

func nestedCallerCalls(res *ParseResult, callerIdx int, callee string) bool {
	for _, c := range res.Calls {
		if c.CallerNodeIdx == callerIdx && c.CalleeName == callee {
			return true
		}
	}
	return false
}

// JS: the NodeBB/express namespace-augmentation pattern. Pre-fix `getCategory` (a nested
// named function hung off a parameter) was invisible AND its calls were mis-attributed to
// the outer module function.
func TestNestedFunctions_JSNamespaceAugmentation(t *testing.T) {
	src := "'use strict';\n" +
		"module.exports = function (Categories) {\n" +
		"  Categories.getCategory = async function (cid) { return validate(cid); };\n" +
		"  [1].forEach(function (x) { register(x); });\n" +
		"};\n" +
		"function validate(c){ return c; }\n" +
		"function register(x){ return x; }\n"
	res := parseFixture(t, "categories.js", src)

	gc := nestedNodeIdx(res, "getCategory")
	if gc < 0 {
		t.Fatalf("nested named function getCategory not exposed as a node; nodes=%v", res.Nodes)
	}
	if !nestedCallerCalls(res, gc, "validate") {
		t.Errorf("getCategory->validate not attributed to the nested function")
	}
	// The anonymous forEach callback's register() must attribute to the ENCLOSING module
	// function (not be lost, not be mis-attributed to getCategory).
	regCaller := -1
	for _, c := range res.Calls {
		if c.CalleeName == "register" {
			regCaller = c.CallerNodeIdx
		}
	}
	if regCaller < 0 {
		t.Errorf("register() in the anonymous callback was LOST (no caller)")
	} else if regCaller == gc {
		t.Errorf("register() mis-attributed to getCategory instead of the enclosing fn")
	}
}

// Python: a nested `def inner()` must be its own node, and its helper() call must attribute
// to inner — NOT to outer (the boundary must hold).
func TestNestedFunctions_PythonNestedDef(t *testing.T) {
	src := "def outer():\n" +
		"    def inner():\n" +
		"        helper()\n" +
		"    return inner\n" +
		"\n" +
		"def helper():\n" +
		"    pass\n"
	res := parseFixture(t, "m.py", src)

	inner := nestedNodeIdx(res, "inner")
	if inner < 0 {
		t.Fatalf("nested def inner not exposed as a node; nodes=%v", res.Nodes)
	}
	if !nestedCallerCalls(res, inner, "helper") {
		t.Errorf("inner->helper not attributed to the nested def")
	}
	if outer := nestedNodeIdx(res, "outer"); outer >= 0 && nestedCallerCalls(res, outer, "helper") {
		t.Errorf("helper() mis-attributed to outer instead of inner (boundary failed)")
	}
}

// Go: a func literal is anonymous (no node), so its call must still attribute to the
// enclosing named function — the anonymous-boundary-not-lost path.
func TestNestedFunctions_GoFuncLiteral(t *testing.T) {
	src := "package p\n\n" +
		"func Outer() {\n" +
		"\tf := func() { helper() }\n" +
		"\tf()\n" +
		"}\n\n" +
		"func helper() {}\n"
	res := parseFixture(t, "m.go", src)

	outer := nestedNodeIdx(res, "Outer")
	if outer < 0 {
		t.Fatalf("Outer not indexed; nodes=%v", res.Nodes)
	}
	if !nestedCallerCalls(res, outer, "helper") {
		t.Errorf("helper() inside a Go func literal was lost — must attribute to Outer")
	}
}
