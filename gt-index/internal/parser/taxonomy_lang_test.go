package parser

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// parseFixture parses one in-memory source file through the ordinary parser
// path, so these tests exercise exactly what an index run does.
func parseTaxonomyFixture(t *testing.T, name, source string) *ParseResult {
	t.Helper()
	dir := t.TempDir()
	abs := filepath.Join(dir, name)
	if err := os.WriteFile(abs, []byte(source), 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	spec := specs.ForExtension(filepath.Ext(name))
	if spec == nil {
		t.Fatalf("no spec registered for %q", filepath.Ext(name))
	}
	sf := walker.SourceFile{Path: name, AbsPath: abs, Language: spec.Name, Spec: spec}
	result, err := ParseFile(sf, false)
	if err != nil {
		t.Fatalf("parse %s: %v", name, err)
	}
	return result
}

// symbolKinds returns the distinct symbol_kind values in a parse result.
func symbolKinds(result *ParseResult) []string {
	seen := map[string]bool{}
	for _, p := range result.Properties {
		if p.Kind == PropSymbolKind {
			seen[p.Value] = true
		}
	}
	out := make([]string, 0, len(seen))
	for k := range seen {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func propValues(result *ParseResult, kind string) []string {
	var out []string
	for _, p := range result.Properties {
		if p.Kind == kind {
			out = append(out, p.Value)
		}
	}
	sort.Strings(out)
	return out
}

func labelCounts(result *ParseResult) map[string]int {
	out := map[string]int{}
	for _, n := range result.Nodes {
		out[n.Label]++
	}
	return out
}

func assertKinds(t *testing.T, result *ParseResult, want ...string) {
	t.Helper()
	got := symbolKinds(result)
	sort.Strings(want)
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("symbol kinds = %v, want %v", got, want)
	}
}

// ── Per-language fixtures ────────────────────────────────────────────────
//
// One fixture per grammar that has code declarations, asserting exactly which
// kinds the taxonomy produces. "Exactly" matters: an over-claiming mapping is
// the failure this item must not ship, so these compare the whole set rather
// than checking membership.

func TestTaxonomyKindsTypeScript(t *testing.T) {
	result := parseTaxonomyFixture(t, "a.ts", `
export interface Shape { area(): number }
export enum Color { Red = "red", Green = "green" }
export type Alias = Shape | Color;
export namespace Geo { export const k = 1; }
export class Circle implements Shape {
  constructor(private r: number) {}
  get radius(): number { return this.r }
  area(): number { return 3 * this.r }
}
export function build(): Circle { return new Circle(1) }
`)
	// Enum, EnumMember, TypeAlias and Namespace are mapped and emitted by
	// emitTaxonomyDeclaration.
	assertKinds(t, result,
		specs.KindInterface, specs.KindClass,
		specs.KindConstructor, specs.KindAccessor, specs.KindMethod,
		specs.KindFunction, specs.KindEnum, specs.KindEnumMember,
		specs.KindTypeAlias, specs.KindNamespace,
	)

	labels := labelCounts(result)
	for _, label := range []string{"Enum", "EnumMember", "TypeAlias", "Namespace"} {
		if labels[label] == 0 {
			t.Errorf("%s declaration node was not emitted; labels=%v", label, labels)
		}
	}

	// IMPLEMENTS is read from TypeScript's dedicated implements_clause.
	if got := propValues(result, PropImplementsType); len(got) != 1 ||
		got[0] != "Shape|"+specs.MechImplementsClause {
		t.Errorf("implements_type = %v, want [Shape|%s]", got, specs.MechImplementsClause)
	}
}

func TestTaxonomyKindsGo(t *testing.T) {
	result := parseTaxonomyFixture(t, "a.go", `
package p

type Point struct{ X int }
type Stringer interface{ String() string }
type Celsius = float64
type Miles float64

func New() *Point { return &Point{} }
func (p *Point) String() string { return "p" }
`)
	// Go's kind is read structurally from the node nested in type_declaration,
	// so `type Celsius = float64` is an alias and `type Miles float64` is a
	// named type that also falls to the alias default -- neither is a struct.
	assertKinds(t, result,
		specs.KindStruct, specs.KindInterface, specs.KindTypeAlias,
		specs.KindFunction, specs.KindMethod,
	)
	if labels := labelCounts(result); labels["Interface"] != 1 {
		t.Errorf("expected the pre-existing Go Interface label to be untouched, labels=%v", labels)
	}
}

func TestTaxonomyKindsRust(t *testing.T) {
	result := parseTaxonomyFixture(t, "a.rs", `
pub mod routing;
pub type Alias = u32;
pub const MAX: u32 = 3;
pub struct Router { n: u32 }
pub enum Mode { Fast, Slow }
pub trait Handle { fn go(&self); }
impl Handle for Router { fn go(&self) {} }
pub union Raw { a: u32, b: f32 }
macro_rules! shout { () => {} }
`)
	// Module, TypeAlias, Constant, Union and Macro come from the additive
	// declaration emitter; struct, enum, trait and impl are separated from the
	// single Class label they all shared, which needs no new node.
	assertKinds(t, result,
		specs.KindStruct, specs.KindEnum, specs.KindTrait, specs.KindImpl,
		specs.KindFunction, specs.KindModule, specs.KindTypeAlias,
		specs.KindConstant, specs.KindUnion, specs.KindMacro,
	)
	// `impl Handle for Router` states conformance in a named field.
	if got := propValues(result, PropImplementsType); len(got) != 1 ||
		got[0] != "Handle|"+specs.MechImplTrait {
		t.Errorf("implements_type = %v, want [Handle|%s]", got, specs.MechImplTrait)
	}
}

func TestTaxonomyKindsJava(t *testing.T) {
	result := parseTaxonomyFixture(t, "A.java", `
interface Shape { double area(); }
enum Color { RED, GREEN }
record Pair(int a, int b) {}
@interface Marker {}
class Circle implements Shape {
  Circle() {}
  @Override public double area() { return 1.0; }
}
`)
	assertKinds(t, result,
		specs.KindInterface, specs.KindEnum, specs.KindClass,
		specs.KindConstructor, specs.KindMethod, specs.KindEnumMember,
		specs.KindRecord, specs.KindAnnotation,
	)
	if got := propValues(result, PropImplementsType); len(got) != 1 ||
		got[0] != "Shape|"+specs.MechImplementsClause {
		t.Errorf("implements_type = %v, want [Shape|%s]", got, specs.MechImplementsClause)
	}
	if got := propValues(result, PropOverrideMarker); len(got) != 1 {
		t.Errorf("override_marker = %v, want exactly one @Override witness", got)
	}
}

func TestTaxonomyKindsPython(t *testing.T) {
	result := parseTaxonomyFixture(t, "a.py", `
class Shape:
    def __init__(self):
        pass
    def area(self):
        return 1

def build():
    return Shape()
`)
	// Python's declaration set is deliberately narrow: an Enum is a class, a
	// Protocol is a class, a dataclass is a class, and a method is the same
	// function_definition node as a free function. The mapping claims none of
	// them, and this fixture pins that restraint: `area` is a Method by LABEL
	// (GT already records the parent) but its KIND is `function`, because that
	// is what the grammar declared.
	assertKinds(t, result, specs.KindClass, specs.KindConstructor,
		specs.KindFunction)
	if labels := labelCounts(result); labels["Method"] != 2 {
		t.Errorf("Method label = %d, want 2 (unchanged by item 11); labels=%v", labels["Method"], labels)
	}
	if got := propValues(result, PropImplementsType); len(got) != 0 {
		t.Errorf("python must claim no IMPLEMENTS, got %v", got)
	}
}

func TestTaxonomyKindsCpp(t *testing.T) {
	result := parseTaxonomyFixture(t, "a.cpp", `
namespace geo {
enum Color { Red, Green };
union Raw { int a; float b; };
typedef int Meters;
using Alias = int;
struct Point { int x; };
class Circle { public: double area() const { return 1.0; } };
}
double area() { return 1.0; }
`)
	assertKinds(t, result,
		specs.KindStruct, specs.KindClass, specs.KindFunction,
		specs.KindNamespace, specs.KindEnum, specs.KindEnumMember,
		specs.KindUnion, specs.KindTypeAlias,
	)
}

func TestTaxonomyKindsCSharp(t *testing.T) {
	result := parseTaxonomyFixture(t, "A.cs", `
namespace Geo {
  public interface IShape { double Area(); }
  public enum Color { Red, Green }
  public record Pair(int A, int B);
  public struct Point { public int X; }
  public class Circle : IShape {
    public Circle() {}
    public override string ToString() { return "c"; }
    public double Area() { return 1.0; }
  }
}
`)
	assertKinds(t, result,
		specs.KindInterface, specs.KindStruct,
		specs.KindClass, specs.KindConstructor, specs.KindMethod,
		specs.KindNamespace, specs.KindEnum, specs.KindEnumMember,
		specs.KindRecord,
	)
	// base_list does not say which entry is an interface, so C# claims none.
	if got := propValues(result, PropImplementsType); len(got) != 0 {
		t.Errorf("csharp must claim no IMPLEMENTS, got %v", got)
	}
	if got := propValues(result, PropOverrideMarker); len(got) != 1 {
		t.Errorf("override_marker = %v, want exactly one `override` witness", got)
	}
}

func TestTaxonomyKindsPHP(t *testing.T) {
	result := parseTaxonomyFixture(t, "a.php", `<?php
namespace App;
interface Shape { public function area(); }
trait Loggable { public function log() {} }
enum Color { case Red; case Green; }
class Circle implements Shape {
  public function __construct() {}
  public function area() { return 1; }
}
function build() { return new Circle(); }
`)
	assertKinds(t, result,
		specs.KindInterface, specs.KindClass,
		specs.KindConstructor, specs.KindMethod, specs.KindFunction,
		specs.KindNamespace, specs.KindTrait, specs.KindEnum,
		specs.KindEnumMember,
	)
	if got := propValues(result, PropImplementsType); len(got) != 1 ||
		got[0] != "Shape|"+specs.MechImplementsClause {
		t.Errorf("implements_type = %v, want [Shape|%s]", got, specs.MechImplementsClause)
	}
}

func TestTaxonomyKindsSwiftStructIsNotAClass(t *testing.T) {
	// MEASURED: the pinned Swift grammar has no struct_declaration node type.
	// A struct, an enum and an extension are all class_declaration, which is
	// why every Swift type looked like a class to GT until now.
	result := parseTaxonomyFixture(t, "a.swift", `
protocol Shape { func area() -> Double }
struct Point { var x: Int }
enum Color { case red }
class Circle { func area() -> Double { return 1.0 } }
extension Circle { func twice() -> Double { return 2.0 } }
typealias Meters = Double
func build() -> Circle { return Circle() }
`)
	got := symbolKinds(result)
	joined := strings.Join(got, ",")
	for _, want := range []string{specs.KindStruct, specs.KindEnum, specs.KindImpl,
		specs.KindProtocol, specs.KindClass} {
		if !strings.Contains(joined, want) {
			t.Errorf("swift kinds %v missing %q", got, want)
		}
	}
}

// TestTaxonomyKindsElixirReadsTheDeclaringCall pins two things at once: the
// keyword refinement (Elixir has no declaration node types at all -- every
// definition is a `call`, and only the leading word says which form it is),
// and a pre-existing limit this item does not change. walkNode returns without
// recursing once it emits a function node, and `defmodule` IS that node, so a
// module body contributes exactly one symbol. Before item 11 that symbol was
// labelled Function with no further information; now it is correctly a module.
func TestTaxonomyKindsElixirReadsTheDeclaringCall(t *testing.T) {
	result := parseTaxonomyFixture(t, "a.ex", `
defmodule Router do
  defstruct [:path]
  def go(x) do
    x
  end
end
`)
	assertKinds(t, result, specs.KindModule)
	if len(result.Nodes) != 1 {
		t.Errorf("nodes = %d, want 1 (the parser does not descend past the outermost call)", len(result.Nodes))
	}
	// Whole-word matching: `def` must not have matched inside `defmodule`.
	if got := symbolKinds(result); len(got) != 1 || got[0] != specs.KindModule {
		t.Errorf("kinds = %v, want [module]; `def` leaked into `defmodule`", got)
	}
}

// TestEveryEmittedSymbolCarriesExactlyOneKind is the invariant that keeps the
// taxonomy queryable: a consumer joining nodes to properties on symbol_kind
// must not see a node twice, and must not see a node with no kind at all.
func TestEveryEmittedSymbolCarriesExactlyOneKind(t *testing.T) {
	fixtures := []struct{ name, src string }{
		{"a.ts", "export enum E { A }\nexport class C { m() {} }\nexport function f() {}\n"},
		{"a.go", "package p\ntype T struct{}\nfunc F() {}\nfunc (t T) M() {}\n"},
		{"a.rs", "pub mod m;\npub struct S;\nimpl S { fn go(&self) {} }\n"},
		{"A.java", "class C { void m() {} }\nenum E { A }\n"},
		{"a.py", "class C:\n    def m(self):\n        pass\n"},
		{"a.php", "<?php\nclass C { public function m() {} }\n"},
	}
	for _, f := range fixtures {
		result := parseTaxonomyFixture(t, f.name, f.src)
		counts := make([]int, len(result.Nodes))
		for _, p := range result.Properties {
			if p.Kind != PropSymbolKind {
				continue
			}
			if p.NodeIdx < 0 || p.NodeIdx >= len(counts) {
				t.Fatalf("%s: symbol_kind row points at node %d of %d", f.name, p.NodeIdx, len(counts))
			}
			counts[p.NodeIdx]++
		}
		for i, c := range counts {
			if c != 1 {
				t.Errorf("%s: node %d (%s %q) carries %d symbol_kind rows, want 1",
					f.name, i, result.Nodes[i].Label, result.Nodes[i].Name, c)
			}
		}
	}
}

// TestTaxonomyNeverRelabelsAnExistingNode pins the additivity claim directly:
// for a fixture whose declarations the parser already emitted before this
// item, the count under every pre-existing label is unchanged, and the only
// new labels are ones no pre-existing node uses.
func TestTaxonomyNeverRelabelsAnExistingNode(t *testing.T) {
	result := parseTaxonomyFixture(t, "a.ts", `
export interface Shape { area(): number }
export enum Color { Red }
export type Alias = Shape;
export class Circle implements Shape { area(): number { return 1 } }
export function build(): Circle { return new Circle() }
`)
	labels := labelCounts(result)
	// Pre-existing labels, with the counts the parser produced before item 11:
	// one Class (Circle), one Class (Shape, since interface_declaration is in
	// the TypeScript spec's ClassNodes), one Method, one Function.
	if labels["Class"] != 2 {
		t.Errorf("Class = %d, want 2 (Shape and Circle, as before item 11); labels=%v", labels["Class"], labels)
	}
	if labels["Method"] != 1 {
		t.Errorf("Method = %d, want 1; labels=%v", labels["Method"], labels)
	}
	if labels["Function"] != 1 {
		t.Errorf("Function = %d, want 1; labels=%v", labels["Function"], labels)
	}
	for _, label := range []string{"Enum", "EnumMember", "TypeAlias"} {
		if labels[label] == 0 {
			t.Errorf("missing additive taxonomy label %q; labels=%v", label, labels)
		}
	}
}

func TestPreviouslyUnreachableLanguageDeclarationsAreEmitted(t *testing.T) {
	cases := []struct {
		name, src, label, symbol string
	}{
		{"a.lua", "function greet(n)\n  return n\nend\n", "Function", "greet"},
		{"a.sql", "CREATE TABLE users (id int);\nCREATE FUNCTION lookup(x int) RETURNS int AS 'x' LANGUAGE SQL;\n", "Table", "users"},
		{"a.svelte", "<main><h1>Hello</h1></main>\n", "Element", "main"},
	}
	for _, c := range cases {
		result := parseTaxonomyFixture(t, c.name, c.src)
		found := false
		for _, node := range result.Nodes {
			if node.Label == c.label && node.Name == c.symbol && node.ByteEnd > node.ByteStart {
				found = true
			}
		}
		if !found {
			t.Errorf("%s: missing %s %q with content address; nodes=%+v", c.name, c.label, c.symbol, result.Nodes)
		}
	}
}

// TestDeclarationEmitterWorks exercises the additive declaration path directly.
func TestDeclarationEmitterWorks(t *testing.T) {
	original := EmitTaxonomyDeclarations
	EmitTaxonomyDeclarations = true
	defer func() { EmitTaxonomyDeclarations = original }()

	result := parseTaxonomyFixture(t, "a.ts", `
export enum Color { Red = "red", Green = "green" }
export type Alias = Color;
export namespace Geo { export const k = 1; }
`)
	labels := labelCounts(result)
	for _, want := range []struct {
		label string
		n     int
	}{{"Enum", 1}, {"EnumMember", 2}, {"TypeAlias", 1}, {"Namespace", 1}} {
		if labels[want.label] != want.n {
			t.Errorf("%s = %d, want %d; labels=%v", want.label, labels[want.label], want.n, labels)
		}
	}
	assertKinds(t, result, specs.KindEnum, specs.KindEnumMember,
		specs.KindTypeAlias, specs.KindNamespace)

	rust := parseTaxonomyFixture(t, "a.rs", `
pub mod routing;
pub type Alias = u32;
pub const MAX: u32 = 3;
pub union Raw { a: u32, b: f32 }
macro_rules! shout { () => {} }
`)
	got := strings.Join(symbolKinds(rust), ",")
	for _, want := range []string{specs.KindModule, specs.KindTypeAlias,
		specs.KindConstant, specs.KindUnion, specs.KindMacro} {
		if !strings.Contains(got, want) {
			t.Errorf("rust kinds %s missing %q", got, want)
		}
	}
}

// TestDeclarationEmissionIsOnByDefault states the shipped default explicitly.
func TestDeclarationEmissionIsOnByDefault(t *testing.T) {
	if !EmitTaxonomyDeclarations {
		t.Error("EmitTaxonomyDeclarations is off; final-hardening taxonomy is incomplete")
	}
}
