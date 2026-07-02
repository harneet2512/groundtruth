package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ---------------------------------------------------------------------------
// B1 — Strategy 1.5 (import-verified) must be RECEIVER-AWARE.
// A QUALIFIED method call `cache.get()` must NOT bind the imported FREE function
// `get` (from `from lib.http import get`) at method="import"/1.0/CERTIFIED — an
// attribute call on a receiver can never bind a bare imported symbol; only the
// receiver-typing rungs can say what cache.get resolves to. Only UNQUALIFIED
// calls, the whole-module "*" case, and the package-alias branch are receiver-safe.
// Mutation: running the bare-name import lookups for a qualified call re-mints the
// import/CERTIFIED launder (RED). This is the last known CERTIFIED-laundering path.
// ---------------------------------------------------------------------------
func TestResolve_B1_QualifiedCallDoesNotBindImportedBareName(t *testing.T) {
	files := []string{"app/caller.py", "lib/http.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	// caller SPECIFICALLY imports the free functions get + post from lib.http.
	imports := []parser.ImportRef{
		{ImportedName: "get", ModulePath: "lib.http", File: "app/caller.py", Line: 1},
		{ImportedName: "post", ModulePath: "lib.http", File: "app/caller.py", Line: 2},
	}
	calls := []parser.CallRef{
		// QUALIFIED: cache.get() — a method on the `cache` receiver, NOT the bare import.
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "cache.get", Line: 5, File: "app/caller.py"},
		// UNQUALIFIED positive control: post() — must STILL resolve via import.
		{CallerNodeIdx: 0, CalleeName: "post", CalleeQualified: "post", Line: 6, File: "app/caller.py"},
	}
	nodeIDs := map[string][]int64{"caller": {1}, "get": {2}, "post": {4}}
	fileNodeIDs := map[string]map[string][]int64{
		"app/caller.py": {"caller": {1}},
		"lib/http.py":   {"get": {2}, "post": {4}},
	}
	callerIDs := []int64{1, 1}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "caller", File: "app/caller.py"},
		2: {Label: "Function", Name: "get", File: "lib/http.py"},
		4: {Label: "Function", Name: "post", File: "lib/http.py"},
	}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	byTarget := map[int64]ResolvedCall{}
	for _, r := range resolved {
		byTarget[r.TargetNodeID] = r
	}

	// NEGATIVE (B1): the qualified cache.get() must NOT launder the free function
	// get(2) as a CERTIFIED import fact.
	if r, ok := byTarget[2]; ok {
		if r.Method == "import" || r.TrustTier == "CERTIFIED" {
			t.Errorf("B1: qualified cache.get() laundered imported free function get(2) as a fact "+
				"(method=%q conf=%.2f tier=%q); a receiver attribute call must not bind a bare imported symbol",
				r.Method, r.Confidence, r.TrustTier)
		}
	}

	// POSITIVE control: the UNQUALIFIED post() must STILL resolve via import (the
	// bare-name path is unchanged for unqualified calls).
	if r, ok := byTarget[4]; !ok || r.Method != "import" {
		t.Errorf("unqualified post() must still resolve via import (Block A unchanged for unqualified); got %+v", r)
	}
}

// B1-followup guard — with the namespace ALIAS present (parser now emits it for
// `import * as helpers from './helpers'`), a QUALIFIED `helpers.foo()` must resolve to
// helpers.foo via the package-alias branch at method="import" — B1's receiver-blind
// guard must NOT degrade this legitimate namespace call to name_match (the regression
// the adversarial re-audit flagged).
func TestResolve_B1_NamespaceAliasQualifiedResolvesViaImport(t *testing.T) {
	fm := BuildFileMap([]string{"m.ts", "helpers.ts"}, []string{"typescript", "typescript"})
	imports := []parser.ImportRef{
		{ImportedName: "*", ModulePath: "./helpers", File: "m.ts", Line: 1},
		{ImportedName: "helpers", ModulePath: "./helpers", File: "m.ts", Line: 1},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "foo", CalleeQualified: "helpers.foo", Line: 5, File: "m.ts"},
	}
	nodeIDs := map[string][]int64{"run": {1}, "foo": {2}}
	fileNodeIDs := map[string]map[string][]int64{
		"m.ts":       {"run": {1}},
		"helpers.ts": {"foo": {2}},
	}
	callerIDs := []int64{1}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "run", File: "m.ts"},
		2: {Label: "Function", Name: "foo", File: "helpers.ts"},
	}
	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	var got *ResolvedCall
	for i := range resolved {
		if resolved[i].TargetNodeID == 2 {
			got = &resolved[i]
		}
	}
	if got == nil {
		t.Fatalf("helpers.foo() did not resolve to foo(2); resolved=%+v", resolved)
	}
	if got.Method != "import" {
		t.Errorf("namespace-aliased helpers.foo() resolved as method=%q conf=%.2f; want import via the "+
			"package-alias branch — B1 must not degrade the legit namespace case", got.Method, got.Confidence)
	}
}
