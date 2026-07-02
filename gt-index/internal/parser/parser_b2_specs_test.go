package parser

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
)

// parseFixture is shared with parser_idxfix_test.go (same package). B2 tests reuse it.

func nodeNames(res *ParseResult) []string {
	out := make([]string, 0, len(res.Nodes))
	for _, n := range res.Nodes {
		out = append(out, n.Name)
	}
	return out
}

func hasNode(res *ParseResult, name string) bool {
	for _, n := range res.Nodes {
		if n.Name == name {
			return true
		}
	}
	return false
}

func importNames(res *ParseResult) []string {
	out := make([]string, 0, len(res.Imports))
	for _, imp := range res.Imports {
		out = append(out, imp.ImportedName)
	}
	return out
}

func contains(ss []string, want string) bool {
	for _, s := range ss {
		if s == want {
			return true
		}
	}
	return false
}

// FIX 1 — TypeScript function_expression parity.
// Red before: "function_expression" absent from the TS FunctionNodes list, so a
// var-assigned anonymous function expression is never indexed → no node "f".
func TestB2_TS_FunctionExpression(t *testing.T) {
	res := parseFixture(t, "a.ts", `const f = function(){ g(); }`)
	if !hasNode(res, "f") {
		t.Fatalf("expected a node named %q; got nodes %v", "f", nodeNames(res))
	}
}

// FIX 2 — Rust grouped/nested use imports.
// Red before: the string-based parser split on the first "}" and emitted a
// garbled composite "b::{C" and dropped "e".
func TestB2_Rust_GroupedUse(t *testing.T) {
	res := parseFixture(t, "b.rs", `use a::{b::{C, D}, e};`)
	names := importNames(res)
	for _, want := range []string{"C", "D", "e"} {
		if !contains(names, want) {
			t.Errorf("expected import leaf %q; got %v", want, names)
		}
	}
	// No garbled composite names.
	for _, got := range names {
		if got == "" || got == "b::{C" || len(got) > 3 {
			t.Errorf("garbled/unexpected import name %q in %v", got, names)
		}
	}
}

// FIX 3 — TypeScript `import x = require("mod");` (import_require_clause).
// Red before: the string lives inside import_require_clause (not the "source"
// field), so extractJSTSImports found no source and returned nothing.
func TestB2_TS_ImportRequire(t *testing.T) {
	res := parseFixture(t, "c.ts", `import x = require("mod");`)
	found := false
	for _, imp := range res.Imports {
		if imp.ImportedName == "x" && imp.ModulePath == "mod" {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected ImportRef x -> mod; got imports %+v", res.Imports)
	}
}

// FIX 4 — Kotlin half-graph (zero named fields).
// Red before: NameField "simple_identifier" is a node TYPE not a field, so the
// name resolved to "" and the function was dropped; and BodyField "function_body"
// is also a node type, so no calls were extracted.
func TestB2_Kotlin_HalfGraph(t *testing.T) {
	res := parseFixture(t, "d.kt", "fun foo() { bar() }")
	if !hasNode(res, "foo") {
		t.Fatalf("expected a node named %q; got nodes %v", "foo", nodeNames(res))
	}
	// A CALLS edge foo -> bar: find node "foo" and a CallRef from it to "bar".
	fooIdx := -1
	for i, n := range res.Nodes {
		if n.Name == "foo" {
			fooIdx = i
			break
		}
	}
	if fooIdx < 0 {
		t.Fatalf("no foo node")
	}
	fooToBar := false
	for _, c := range res.Calls {
		if c.CallerNodeIdx == fooIdx && c.CalleeName == "bar" {
			fooToBar = true
		}
	}
	if !fooToBar {
		t.Fatalf("expected a CALLS edge foo->bar; got calls %+v", res.Calls)
	}
}

// FIX 2 regression guard: `pub use` re-exports must ALSO emit ReExportRef through the AST
// walk. The grouped-use rewrite (on an old base) initially emitted only ImportRef, silently
// dropping Rust barrel re-export tracking that the resolver's ChainReExports depends on.
func TestB2_Rust_PubUseEmitsReExport(t *testing.T) {
	src := "pub use crate::foo::Bar;\n" +
		"pub use a::{b::C, d};\n" + // nested grouped pub use
		"use only::Imported;\n" // plain use → NOT a re-export
	res := parseFixture(t, "lib.rs", src)
	reexported := map[string]bool{}
	for _, r := range res.ReExports {
		reexported[r.ExportedName] = true
	}
	for _, want := range []string{"Bar", "C", "d"} {
		if !reexported[want] {
			t.Errorf("pub use should re-export %q; ReExports=%+v", want, res.ReExports)
		}
	}
	if reexported["Imported"] {
		t.Errorf("plain `use` must NOT emit a ReExportRef; ReExports=%+v", res.ReExports)
	}
}

// FIX 4 (detection predicate) — the zero-named-fields warning fires on grammars
// whose FieldName(1) is empty. Kotlin has zero fields; TypeScript does not.
func TestB2_ZeroFieldPredicate(t *testing.T) {
	kt := specs.ForExtension(".kt")
	if kt == nil || kt.Language.FieldName(1) != "" {
		t.Errorf("expected Kotlin to expose zero named fields (FieldName(1)==\"\"); got %q", kt.Language.FieldName(1))
	}
	ts := specs.ForExtension(".ts")
	if ts == nil || ts.Language.FieldName(1) == "" {
		t.Errorf("expected TypeScript to expose named fields (FieldName(1)!=\"\")")
	}
}
