package resolver

import (
	"os"
	"strings"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ---------------------------------------------------------------------------
// B2 — methodsByClass must be built DETERMINISTICALLY.
// Go map iteration is randomized. When a class has >=2 members with the SAME name
// (conditional defs, overloads, cfg twins), the old `for id, m := range nodeMeta[0]`
// last-writer-wins picks a run-dependent winner → the resolved self.method() CALLS
// target flips between indexings (the documented ~0.5% textual/wasmi drift). The
// winner must be the stable min-(file,line,id). Mutation: reverting to the map-range
// build makes the winner flip across the N runs below (RED, reliably within N=64).
// ---------------------------------------------------------------------------
func TestResolve_B2_MethodsByClassDeterministicOnDuplicateName(t *testing.T) {
	fm := BuildFileMap([]string{"a.py"}, []string{"python"})

	// Class C(10) has TWO methods named foo: id 2 @line5 and id 3 @line9. caller(1) is
	// a method of C and calls self.foo(). min-(file,line,id) => id 2 (line 5).
	nodeIDs := map[string][]int64{"caller": {1}, "foo": {2, 3}, "C": {10}}
	fileNodeIDs := map[string]map[string][]int64{
		"a.py": {"caller": {1}, "foo": {2, 3}, "C": {10}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", Name: "caller", File: "a.py", ParentID: 10, StartLine: 2},
		10: {Label: "Class", Name: "C", File: "a.py", StartLine: 1},
		2:  {Label: "Method", Name: "foo", File: "a.py", ParentID: 10, StartLine: 5},
		3:  {Label: "Method", Name: "foo", File: "a.py", ParentID: 10, StartLine: 9},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "foo", CalleeQualified: "self.foo", Line: 3, File: "a.py"},
	}
	callerIDs := []int64{1}

	const N = 64
	var first int64 = -1
	for i := 0; i < N; i++ {
		resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
		var got int64 = -1
		for _, r := range resolved {
			if r.SourceNodeID == 1 && (r.TargetNodeID == 2 || r.TargetNodeID == 3) {
				got = r.TargetNodeID
			}
		}
		if got == -1 {
			t.Fatalf("run %d: self.foo() did not resolve to either foo method (2 or 3); resolved=%+v", i, resolved)
		}
		if first == -1 {
			first = got
		} else if got != first {
			t.Fatalf("run %d: self.foo() target flipped between runs (%d vs %d) — methodsByClass build is nondeterministic", i, got, first)
		}
	}
	if first != 2 {
		t.Errorf("self.foo() resolved to id %d; want the stable min-(file,line,id) winner id 2 (a.py:5)", first)
	}
}

// ---------------------------------------------------------------------------
// FIX 1 — Go receiver self-call rung (Strategy 1.75).
// Inside `func (r *T) M()`, a call `r.helper()` (r = the method's receiver var,
// supplied structurally in NodeMeta.ReceiverName) is the Go analogue of self.method()
// and must resolve to T.helper as a FACT (same_file / conf 1.0) — NOT a receiver-
// unproven guess (unique_method 0.6) and NOT a name_match. The receiver TYPE also
// disambiguates: two types (T, T2) both define `helper`; the self-call must pick the
// CALLER'S type's helper (id 2), never T2's (id 3).
// Mutation (RED): without the receiver-var clause, `r` is not self/this/Self, so 1.75
// is skipped and the two-candidate `helper` falls through to name_match.
// ---------------------------------------------------------------------------
func TestResolve_B2_GoReceiverSelfCallResolvesAsFact(t *testing.T) {
	fm := BuildFileMap([]string{"t.go", "u.go"}, []string{"go", "go"})

	// T(10) has method M(1, receiver "r") + helper(2). T2(20) also has a helper(3).
	nodeIDs := map[string][]int64{"M": {1}, "helper": {2, 3}, "T": {10}, "T2": {20}}
	fileNodeIDs := map[string]map[string][]int64{
		"t.go": {"M": {1}, "helper": {2}, "T": {10}},
		"u.go": {"helper": {3}, "T2": {20}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", Name: "M", File: "t.go", ParentID: 10, ReceiverName: "r", StartLine: 3},
		2:  {Label: "Method", Name: "helper", File: "t.go", ParentID: 10, StartLine: 8},
		10: {Label: "Class", Name: "T", File: "t.go", StartLine: 1},
		3:  {Label: "Method", Name: "helper", File: "u.go", ParentID: 20, StartLine: 8},
		20: {Label: "Class", Name: "T2", File: "u.go", StartLine: 1},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "helper", CalleeQualified: "r.helper", Line: 5, File: "t.go"},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	var got *ResolvedCall
	for i := range resolved {
		if resolved[i].SourceNodeID == 1 && (resolved[i].TargetNodeID == 2 || resolved[i].TargetNodeID == 3) {
			got = &resolved[i]
		}
	}
	if got == nil {
		t.Fatalf("r.helper() did not resolve to any helper method; resolved=%+v", resolved)
	}
	if got.TargetNodeID != 2 {
		t.Errorf("r.helper() resolved to id %d; want the CALLER's type helper id 2 (receiver-type disambiguation)", got.TargetNodeID)
	}
	if got.Method != "same_file" || got.Confidence != 1.0 {
		t.Errorf("r.helper() resolved as method=%q conf=%.2f; want a FACT same_file/1.00 via the Go receiver self-call rung (RED: name_match / unique_method)", got.Method, got.Confidence)
	}
}

// ---------------------------------------------------------------------------
// FIX 2 — per-language builtin method drop-list.
// A JS `promise.then()` (qualified, receiver type unknown) must NOT emit a name_match
// CALLS edge to a user-defined function named `then` — `then` is a Promise builtin in
// JS/TS. The default (Python-shaped) set does not contain `then`, so this requires the
// per-language set keyed off the .js extension.
// Mutation (RED): with only the Python default set, `then` is not a builtin → the
// qualified-unresolved single candidate is emitted as name_match to `then`(2).
// Regression control: a Python `obj.join()` is STILL dropped (default set intact).
// ---------------------------------------------------------------------------
func TestResolve_B2_JSBuiltinThenNotNameMatched(t *testing.T) {
	// JS: promise.then() must NOT bind the user function then(2).
	fmJS := BuildFileMap([]string{"app.js", "util.js"}, []string{"javascript", "javascript"})
	nodeIDsJS := map[string][]int64{"run": {1}, "then": {2}}
	fileNodeIDsJS := map[string]map[string][]int64{
		"app.js":  {"run": {1}},
		"util.js": {"then": {2}},
	}
	metaJS := map[int64]NodeMeta{
		1: {Label: "Function", Name: "run", File: "app.js"},
		2: {Label: "Function", Name: "then", File: "util.js"},
	}
	callsJS := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "then", CalleeQualified: "promise.then", Line: 5, File: "app.js"},
	}
	for _, r := range Resolve(callsJS, nodeIDsJS, fileNodeIDsJS, []int64{1}, nil, fmJS, metaJS) {
		if r.TargetNodeID == 2 {
			t.Errorf("JS promise.then() emitted a %q edge to user function then(2) conf=%.2f — a JS builtin method must be dropped, not name_matched", r.Method, r.Confidence)
		}
	}

	// Python regression control: obj.join() must STILL be dropped (default set intact).
	fmPy := BuildFileMap([]string{"a.py", "b.py"}, []string{"python", "python"})
	nodeIDsPy := map[string][]int64{"run": {1}, "join": {2}}
	fileNodeIDsPy := map[string]map[string][]int64{
		"a.py": {"run": {1}},
		"b.py": {"join": {2}},
	}
	metaPy := map[int64]NodeMeta{
		1: {Label: "Function", Name: "run", File: "a.py"},
		2: {Label: "Function", Name: "join", File: "b.py"},
	}
	callsPy := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "join", CalleeQualified: "obj.join", Line: 5, File: "a.py"},
	}
	for _, r := range Resolve(callsPy, nodeIDsPy, fileNodeIDsPy, []int64{1}, nil, fmPy, metaPy) {
		if r.TargetNodeID == 2 {
			t.Errorf("Python obj.join() emitted a %q edge to user function join(2) — the default builtin drop-set regressed", r.Method)
		}
	}
}

// ---------------------------------------------------------------------------
// FIX 3 — ChainReExports fixpoint.
// A 3-level barrel chain: src/index.js re-exports * from ./sub (src/sub/index.js),
// which re-exports * from ./leaf (src/sub/leaf.js). After ChainReExports, an import
// through the TOP barrel (key "src") must reach the LEAF file src/sub/leaf.js.
// A single 1-hop pass only registers each barrel's direct source, so the leaf never
// reaches the top — the fixpoint (up to depth 16) is what propagates it.
// Mutation (RED): the original 1-hop body (or a cap of 1) leaves "src" without the leaf.
// ---------------------------------------------------------------------------
func TestChainReExports_B2_TransitiveThreeLevelChain(t *testing.T) {
	files := []string{"src/index.js", "src/sub/index.js", "src/sub/leaf.js"}
	langs := []string{"javascript", "javascript", "javascript"}
	fm := BuildFileMap(files, langs)

	// Outer barrel FIRST in the slice so a single pass provably cannot resolve the
	// leaf (order-independent proof that the fixpoint is required).
	reExports := []parser.ReExportRef{
		{ExportedName: "*", SourceModule: "./sub", File: "src/index.js", Line: 1},
		{ExportedName: "*", SourceModule: "./leaf", File: "src/sub/index.js", Line: 1},
	}
	chained := ChainReExports(fm, reExports, files, langs)
	if chained == 0 {
		t.Fatal("re-export chain did not chain anything")
	}
	if got := fm["src"]; !containsString(got, "src/sub/leaf.js") {
		t.Fatalf("top barrel key \"src\" = %v; want it to transitively reach the leaf src/sub/leaf.js (fixpoint over the 3-level chain)", got)
	}
}

// ---------------------------------------------------------------------------
// FIX 4 — Rust source roots derived generically (no per-repo path literal).
// Behavioral: a crate at ANY non-"core/engine" path resolves its module names via the
// conventional crate `src/` dir (generic "/src/" derivation), and the previously
// hardcoded core/engine layout still resolves (behavior-preserving).
// Source guard (RED): the per-repo literal "core/engine/src/" must no longer appear in
// resolver.go.
// ---------------------------------------------------------------------------
func TestBuildFileMap_B2_RustRootsGenericNoHardcode(t *testing.T) {
	fm := BuildFileMap(
		[]string{
			"myapp/backend/src/handlers.rs",  // arbitrary workspace member crate
			"services/api/src/routes.rs",     // another non-core/engine crate
			"core/engine/src/widget.rs",      // the formerly-hardcoded layout (must still work)
		},
		[]string{"rust", "rust", "rust"},
	)
	for _, want := range []struct{ key, file string }{
		{"handlers", "myapp/backend/src/handlers.rs"},
		{"routes", "services/api/src/routes.rs"},
		{"widget", "core/engine/src/widget.rs"},
	} {
		if !containsString(fm[want.key], want.file) {
			t.Errorf("rust module key %q = %v; want it to contain %s (generic crate-src derivation)", want.key, fm[want.key], want.file)
		}
	}

	// The per-repo hardcode must be gone from the source.
	src, err := os.ReadFile("resolver.go")
	if err != nil {
		t.Fatalf("read resolver.go: %v", err)
	}
	if strings.Contains(string(src), "core/engine/src/") {
		t.Errorf("resolver.go still contains the per-repo literal \"core/engine/src/\" — Rust roots must be derived generically")
	}
}
