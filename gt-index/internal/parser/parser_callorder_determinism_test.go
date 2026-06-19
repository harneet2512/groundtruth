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

// TestCallOrderDeterministic is the Step-1 parity regression tripwire (fix 389d5f2e).
// extractCallOrdering must emit the SAME call_order properties on every parse of the SAME input.
// Before the fix it ranged a Go map (randomized iteration order) then capped at 5 receivers, so
// WHICH 5 sequences survived flickered run-to-run -> non-deterministic graph.db (witnessed live:
// go expr-lang/expr edge count flipped 6497<->6499 as the `vm: push...pop` call_order entered/left
// the cap, flickering the PRECEDES pop<->push pair). The fixture has 7 receivers (> the cap of 5),
// so the cap MUST evict 2 and the surviving 5 must be a STABLE choice. Language-agnostic invariant;
// exercised on Go, the language that surfaced the non-determinism.
func TestCallOrderDeterministic(t *testing.T) {
	src := "package p\n" +
		"func Run() {\n" +
		"    aa.foo(); aa.bar()\n" +
		"    bb.foo(); bb.bar()\n" +
		"    cc.foo(); cc.bar()\n" +
		"    dd.foo(); dd.bar()\n" +
		"    ee.foo(); ee.bar()\n" +
		"    ff.foo(); ff.bar()\n" +
		"    gg.foo(); gg.bar()\n" +
		"}\n"
	dir := t.TempDir()
	path := filepath.Join(dir, "fixture.go")
	if err := os.WriteFile(path, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	spec := specs.ForExtension(".go")
	if spec == nil {
		t.Skip("no go spec registered")
	}
	sf := walker.SourceFile{Path: path, AbsPath: path, Language: spec.Name, Spec: spec}

	callOrder := func() string {
		res, err := ParseFile(sf, false)
		if err != nil {
			t.Fatalf("ParseFile: %v", err)
		}
		var vals []string
		for _, p := range res.Properties {
			if p.Kind == "call_order" {
				vals = append(vals, p.Value)
			}
		}
		sort.Strings(vals) // compare the SET (the bug is which receivers survive, not their print order)
		return strings.Join(vals, "\n")
	}

	first := callOrder()
	if first == "" {
		t.Fatal("no call_order properties emitted from a 7-receiver fixture")
	}
	// The cap MUST bite (7 receivers, cap 5) — else this test cannot detect the eviction non-determinism.
	if n := strings.Count(first, "\n") + 1; n != 5 {
		t.Fatalf("expected exactly 5 capped call_order props (7 receivers, cap 5), got %d:\n%s", n, first)
	}
	// DETERMINISM: 8 parses of the SAME input must yield byte-identical call_order every time.
	// With the pre-fix map-range, the 8 parses pick different 5-of-7 sets with overwhelming probability.
	for i := 0; i < 8; i++ {
		if got := callOrder(); got != first {
			t.Fatalf("call_order NON-DETERMINISTIC at parse %d (map-iteration regression):\nfirst=%q\ngot  =%q", i, first, got)
		}
	}
}
