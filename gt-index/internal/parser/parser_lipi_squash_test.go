package parser

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// ─────────────────────────────────────────────────────────────────────────────
// LIPI squash batch (GRANULAR_LIPI_REVIEW_20260607T2330Z.md) — parser.go items:
//   #2  interior property Line (byte-offset→line; aggregate facts use func start)
//   #42 goReceiverType paren-depth match (not first ')')
//   #43 capital→constructor gated by language (Go uses ViaReturn)
//   #44 literal-receiver: unwrap parenthesized + chain head
// ─────────────────────────────────────────────────────────────────────────────

// TestGoReceiverTypeParenDepth (item #42) — goReceiverType must find the receiver's
// CLOSING paren by balanced matching, not the FIRST ')'. A generic/func-typed
// receiver has an inner ')' that is not the receiver's; the old first-')' slice
// mis-typed it → the method never parented to its struct (the 58%-method-gap fix
// silently no-op'd on exactly the generic-receiver methods). Pure string, no CGO.
func TestGoReceiverTypeParenDepth(t *testing.T) {
	cases := []struct {
		name string
		sig  string
		want string
	}{
		{"pointer receiver", "func (r *RequiredResourceSelector) GetKind() string", "RequiredResourceSelector"},
		{"value receiver", "func (a Account) Name() string", "Account"},
		{"generic receiver", "func (s *Stack[T]) Push(v T)", "Stack"},
		// The load-bearing regression: inner func() error has a ')' BEFORE the
		// receiver's real ')'. First-')' logic returned "func"; depth-matching → "Service".
		{"generic func-typed param", "func (s *Service[K, func() error]) Do()", "Service"},
		{"generic two-arg", "func (m *Map[K, V]) Get(k K) V", "Map"},
		// Non-methods / malformed → "" (correct-or-quiet, no false parent).
		{"plain function", "func Handle(count int) int", ""},
		{"not a func", "type Foo struct{}", ""},
		{"empty", "", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := goReceiverType(tc.sig)
			if got != tc.want {
				t.Errorf("goReceiverType(%q) = %q, want %q", tc.sig, got, tc.want)
			}
		})
	}
}

// TestPropertyLineAttribution (item #2) — the per-hit text-scan extractors
// (config_read, concurrency) must stamp the LINE OF THE HIT, not the function-body
// start line. A config read 4 lines into the body must carry the body-start line +4.
// Tree-sitter parse → requires CGO at build (runs in CI).
func TestPropertyLineAttribution(t *testing.T) {
	// os.getenv on the 5th line of the function body; the func decl is line 1.
	src := "" +
		"def handle(req):\n" + // line 1 (func decl)
		"    a = 1\n" + //         line 2 (body start)
		"    b = 2\n" + //         line 3
		"    c = 3\n" + //         line 4
		"    key = os.getenv(\"DATABASE_URL\")\n" + // line 5 (the hit)
		"    return key\n" //      line 6

	dir := t.TempDir()
	path := filepath.Join(dir, "fixture.py")
	if err := os.WriteFile(path, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	spec := specs.ForExtension(".py")
	if spec == nil {
		t.Skip("no python spec registered")
	}
	sf := walker.SourceFile{Path: path, AbsPath: path, Language: spec.Name, Spec: spec}
	res, err := ParseFile(sf, false)
	if err != nil {
		t.Fatalf("ParseFile: %v", err)
	}

	var cfg *PropertyRef
	for i := range res.Properties {
		p := &res.Properties[i]
		if p.Kind == "config_read" && strings.Contains(p.Value, "DATABASE_URL") {
			cfg = p
			break
		}
	}
	if cfg == nil {
		t.Fatalf("no config_read property for DATABASE_URL (props: %d)", len(res.Properties))
	}
	// The hit is on line 5. Before the fix this was the body-start line (2).
	if cfg.Line != 5 {
		t.Errorf("config_read Line = %d, want 5 (the hit line, not body start)", cfg.Line)
	}
	t.Logf("config_read DATABASE_URL at line %d (expected 5)", cfg.Line)
}

// TestChainedLiteralReceiverDropped (item #44) — a method call on a CHAINED literal
// receiver (`",".join(...).split(...)`) and on a PARENTHESIZED literal (`("a").join()`)
// must be dropped (returns "",""), exactly like the direct literal case, so it never
// becomes a bogus internal name_match edge. Before the fix only the depth-1 receiver
// was checked, so the chained/wrapped forms leaked through.
func TestChainedLiteralReceiverDropped(t *testing.T) {
	cases := []struct {
		name        string
		src         string
		mustNotCall string // a method name that must NOT appear as an outgoing call
	}{
		{
			// "x".strip().split() — outer .split() has a CALL receiver, not a literal.
			name:        "chained string literal",
			src:         "def f():\n    return \"x\".strip().split(\",\")\n",
			mustNotCall: "split",
		},
		{
			// ("a").join(parts) — parenthesized string literal receiver.
			name:        "parenthesized string literal",
			src:         "def f():\n    return (\"a\").join(parts)\n",
			mustNotCall: "join",
		},
	}
	spec := specs.ForExtension(".py")
	if spec == nil {
		t.Skip("no python spec registered")
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, "fixture.py")
			if err := os.WriteFile(path, []byte(tc.src), 0o644); err != nil {
				t.Fatal(err)
			}
			sf := walker.SourceFile{Path: path, AbsPath: path, Language: spec.Name, Spec: spec}
			res, err := ParseFile(sf, false)
			if err != nil {
				t.Fatalf("ParseFile: %v", err)
			}
			for _, c := range res.Calls {
				if c.CalleeName == tc.mustNotCall {
					t.Errorf("literal-receiver call %q leaked as an internal edge (callee=%q)",
						tc.mustNotCall, c.CalleeName)
				}
			}
		})
	}
}

// TestGoCapitalizedCallNotConstructor (item #43) — the capital→constructor heuristic
// (PyCG, Python/JS/TS-shaped) must be gated OFF for Go: an exported Go func is
// Capitalized but is NOT a constructor (`x := Marshal()` returns []byte, not a
// `Marshal` instance). Stamping TypeName="Marshal" would pollute the resolver with a
// non-existent type. The fix routes a Go capitalized bare call down the ViaReturn
// path (bridge through the callee's return type) instead. Python `x = MyClass()` must
// STILL be a constructor (rule unchanged) — this is the contrast that proves the gate
// is by-language, not a blanket removal.
//
// NOTE (pre-existing, OUT OF SCOPE for this item): Go single-var assignment extraction
// is currently a no-op because tree-sitter-go wraps the LHS/RHS of `:=`/`=` in
// `expression_list` nodes, so `left.Type()=="identifier"` is false and no AssignmentRef
// is produced for Go at all (probed live: `x := Marshal()` → 0 assignments, 1 call).
// The Go subtest therefore asserts the INVARIANT (no false constructor for Go) rather
// than a live ViaReturn row; the gate is still required as defense-in-depth for when
// Go expression_list assignment extraction is added (a separate finding).
func TestGoCapitalizedCallNotConstructor(t *testing.T) {
	// Go: a capitalized bare call must NEVER be recorded as a constructor type fact.
	t.Run("go exported func is never a constructor type fact", func(t *testing.T) {
		spec := specs.ForExtension(".go")
		if spec == nil {
			t.Skip("no go spec registered")
		}
		src := "package p\n" +
			"func use() {\n" +
			"    x := Marshal()\n" +
			"    x.Foo()\n" +
			"}\n"
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
		// Invariant: for a Go file, NO assignment may carry a capitalized constructor
		// TypeName with ViaReturn=false. (Today there are 0 Go assignments; this guards
		// against a future regression that adds them WITHOUT the language gate.)
		for _, a := range res.Assignments {
			if !a.ViaReturn && a.TypeName != "" &&
				a.TypeName[0] >= 'A' && a.TypeName[0] <= 'Z' &&
				a.TypeQualified == "" {
				t.Errorf("Go capitalized bare call stamped as constructor TypeName=%q "+
					"(must be ViaReturn): %+v", a.TypeName, a)
			}
		}
		t.Logf("go assignments: %+v (expected none today; gate guards future regressions)", res.Assignments)
	})

	// Python: capitalized bare call is STILL a constructor (rule unchanged) — proves
	// the gate is by-language, not a blanket removal of the heuristic.
	t.Run("python class call is still a constructor", func(t *testing.T) {
		spec := specs.ForExtension(".py")
		if spec == nil {
			t.Skip("no python spec registered")
		}
		src := "def use():\n    x = MyClass()\n    x.foo()\n"
		dir := t.TempDir()
		path := filepath.Join(dir, "fixture.py")
		if err := os.WriteFile(path, []byte(src), 0o644); err != nil {
			t.Fatal(err)
		}
		sf := walker.SourceFile{Path: path, AbsPath: path, Language: spec.Name, Spec: spec}
		res, err := ParseFile(sf, false)
		if err != nil {
			t.Fatalf("ParseFile: %v", err)
		}
		found := false
		for _, a := range res.Assignments {
			if a.VarName == "x" && a.TypeName == "MyClass" && !a.ViaReturn {
				found = true
			}
		}
		if !found {
			t.Errorf("Python `x = MyClass()` should record constructor TypeName=MyClass; assignments=%+v", res.Assignments)
		}
	})
}
