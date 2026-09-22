package parser

import (
	"strings"
	"testing"
)

// W1 (HAR-90 canonical) — parser half of the call-resolution fixes, asserted
// on the CallRef / PropertyRef / ImportRef rows ParseFile emits for REAL source.

type w1CallWant struct {
	callee, qualified, form string
}

func w1HasCall(res *ParseResult, want w1CallWant) bool {
	for _, c := range res.Calls {
		if c.CalleeName == want.callee && c.CalleeQualified == want.qualified && c.DispatchForm == want.form {
			return true
		}
	}
	return false
}

func w1CallsString(res *ParseResult) string {
	var b strings.Builder
	for _, c := range res.Calls {
		b.WriteString("\n  ")
		b.WriteString(c.CalleeName + " q=" + c.CalleeQualified + " form=" + c.DispatchForm)
	}
	return b.String()
}

// Receiver calls in Java / Ruby / PHP / C# / Kotlin / Swift must name the
// METHOD as the callee and carry the receiver as the qualifier, with the same
// "virtual" dispatch form Python/Go/JS/TS receiver calls get.
func TestW1ReceiverCallExtractionPerLanguage(t *testing.T) {
	cases := []struct {
		file, src string
		want      []w1CallWant
	}{
		{"A.java", "class A {\n  void m(Base x) {\n    x.run();\n    Helper.parse();\n    this.h();\n    h();\n  }\n  void h() {}\n}\n",
			[]w1CallWant{{"run", "x.run", "virtual"}, {"parse", "Helper.parse", "virtual"}, {"h", "this.h", "virtual"}, {"h", "h", "static"}}},
		{"a.rb", "def m(x)\n  x.run\n  Foo.bar(1)\n  baz(2)\nend\n",
			[]w1CallWant{{"run", "x.run", "virtual"}, {"bar", "Foo.bar", "virtual"}, {"baz", "baz", "static"}}},
		{"a.php", "<?php\nfunction m($x) {\n  $x->run();\n  Foo::bar();\n  $this->h();\n  baz();\n}\n",
			[]w1CallWant{{"run", "x.run", "virtual"}, {"bar", "Foo::bar", "virtual"}, {"h", "this.h", "virtual"}, {"baz", "baz", "static"}}},
		{"A.cs", "class A {\n  void M(B x) {\n    x.Run();\n    Helper.Parse();\n  }\n}\n",
			[]w1CallWant{{"Run", "x.Run", "virtual"}, {"Parse", "Helper.Parse", "virtual"}}},
		{"a.kt", "fun m(x: B) {\n  x.run()\n  helper()\n}\n",
			[]w1CallWant{{"run", "x.run", "virtual"}, {"helper", "helper", "static"}}},
		{"a.swift", "func m(o: B) {\n  o.run()\n  helper()\n}\n",
			[]w1CallWant{{"run", "o.run", "virtual"}, {"helper", "helper", "static"}}},
	}
	for _, tc := range cases {
		res := parseFixture(t, tc.file, tc.src)
		for _, w := range tc.want {
			if !w1HasCall(res, w) {
				t.Errorf("%s: missing call %+v; got:%s", tc.file, w, w1CallsString(res))
			}
		}
	}
}

func w1ParamProps(res *ParseResult) []string {
	var out []string
	for _, p := range res.Properties {
		if p.Kind == "param" {
			out = append(out, p.Value)
		}
	}
	return out
}

// Declared parameter types must surface as `param` properties of the form
// "name:Type ..." — the input BuildParamTypeIndex keys receiver typing on.
func TestW1DeclaredParameterFactsPerLanguage(t *testing.T) {
	cases := []struct{ file, src, want string }{
		{"u.py", "def use(p: P):\n    return p.go()\n", "p:P [required]"},
		{"u.py", "def use(p: P = None):\n    return p.go()\n", "p:P opt=None"},
		{"u.ts", "function use(p: P): number {\n  return p.go();\n}\n", "p:P [required]"},
		{"U.java", "class U {\n  int use(P p) { return p.go(); }\n}\n", "p:P [required]"},
	}
	for _, tc := range cases {
		res := parseFixture(t, tc.file, tc.src)
		props := w1ParamProps(res)
		found := false
		for _, v := range props {
			if v == tc.want {
				found = true
			}
		}
		if !found {
			t.Errorf("%s %q: param props = %q, want %q", tc.file, tc.src, props, tc.want)
		}
	}
}

// A module-level CommonJS require() binds imports for the whole file, exactly
// as it does inside a function body.
func TestW1ModuleLevelRequireIsAnImport(t *testing.T) {
	src := "const h = require('./h');\nconst { helper, other } = require('./util');\nfunction z() { return h.helper(); }\n"
	res := parseFixture(t, "b.js", src)
	want := map[string]string{"h": "./h", "helper": "./util", "other": "./util"}
	for name, mod := range want {
		ok := false
		for _, imp := range res.Imports {
			if imp.ImportedName == name && imp.ModulePath == mod {
				ok = true
			}
		}
		if !ok {
			t.Errorf("module-level require: no import %s from %s; imports=%+v", name, mod, res.Imports)
		}
	}
	// Inside a function the require is still captured once (no duplicate from
	// the module-level path).
	src2 := "function z() { const h = require('./h'); return h.helper(); }\n"
	res2 := parseFixture(t, "c.js", src2)
	n := 0
	for _, imp := range res2.Imports {
		if imp.ImportedName == "h" && imp.ModulePath == "./h" {
			n++
		}
	}
	if n != 1 {
		t.Errorf("function-level require imports = %d, want exactly 1; imports=%+v", n, res2.Imports)
	}
}

// Ruby `x = C.new` still types x as C now that `C.new` extracts as callee
// "new" with qualifier "C.new".
func TestW1RubyConstructorAssignmentTypesReceiver(t *testing.T) {
	res := parseFixture(t, "a.rb", "def m\n  x = Base.new\n  x.run\nend\n")
	a := findAssignment(res, "x")
	if a == nil || a.TypeName != "Base" || a.ViaReturn {
		t.Fatalf("x binding = %+v, want constructor type Base", a)
	}
}
