package resolver

import (
	"os"
	"sort"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ---------------------------------------------------------------------------
// B1 NEGATIVE EVIDENCE (GT_NEG_EVIDENCE, default off).
//
// When a bare call's name is BOUND BY AN IMPORT in the caller file whose module
// resolves to NO indexed project file (lodash `merge`, `extpkg` `y`), a cross-file
// name_match onto a same-named PROJECT symbol is PROVABLY WRONG, not merely unproven.
// OFF: the resolver mints that (wrong) name_match — byte-identical to today.
// ON:  the resolver DROPS it (correct-or-quiet), the same move as the stdlib-shadow
//      fix extended from qualified to bare names.
//
// Guardrails proven here: byte-identical-off (OFF == pre-change edge set), no over-drop
// (a genuinely-local name_match + a same-file call survive both modes), determinism
// (two indexings identical, both modes), ≥2 languages (JS + Python). The MUTATION
// companion (TestResolve_B1NegEv_Mutation_*) reddens when the external-bound check is
// neutered so a NOT-imported name is wrongly dropped.
// ---------------------------------------------------------------------------

// edgeTuple is the comparable projection of a resolved CALLS edge.
type edgeTuple struct {
	src, tgt int64
	method   string
	conf     float64
}

func edgeSet(resolved []ResolvedCall) []edgeTuple {
	out := make([]edgeTuple, 0, len(resolved))
	for _, r := range resolved {
		out = append(out, edgeTuple{r.SourceNodeID, r.TargetNodeID, r.Method, r.Confidence})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].src != out[j].src {
			return out[i].src < out[j].src
		}
		if out[i].tgt != out[j].tgt {
			return out[i].tgt < out[j].tgt
		}
		return out[i].method < out[j].method
	})
	return out
}

func hasEdge(resolved []ResolvedCall, src, tgt int64) (ResolvedCall, bool) {
	for _, r := range resolved {
		if r.SourceNodeID == src && r.TargetNodeID == tgt {
			return r, true
		}
	}
	return ResolvedCall{}, false
}

// jsFixture builds a repo: app/main.js imports {merge} from the EXTERNAL 'lodash'
// and calls merge() bare; util/merge.js defines a same-named LOCAL merge. main.js also
// calls sharedHelper() (NOT imported → legit name_match) and a same-file helperLocal().
func jsFixture() (calls []parser.CallRef, nodeIDs map[string][]int64,
	fileNodeIDs map[string]map[string][]int64, callerIDs []int64,
	imports []parser.ImportRef, fm map[string][]string, meta map[int64]NodeMeta) {

	files := []string{"app/main.js", "util/merge.js", "util/shared.js"}
	langs := []string{"javascript", "javascript", "javascript"}
	fm = BuildFileMap(files, langs)

	imports = []parser.ImportRef{
		// merge is bound to the EXTERNAL module lodash (no indexed file).
		{ImportedName: "merge", ModulePath: "lodash", File: "app/main.js", Line: 1},
	}
	calls = []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "merge", CalleeQualified: "merge", Line: 5, File: "app/main.js"},
		{CallerNodeIdx: 0, CalleeName: "sharedHelper", CalleeQualified: "sharedHelper", Line: 6, File: "app/main.js"},
		{CallerNodeIdx: 0, CalleeName: "helperLocal", CalleeQualified: "helperLocal", Line: 7, File: "app/main.js"},
	}
	nodeIDs = map[string][]int64{
		"run": {1}, "merge": {2}, "sharedHelper": {3}, "helperLocal": {4},
	}
	fileNodeIDs = map[string]map[string][]int64{
		"app/main.js":    {"run": {1}, "helperLocal": {4}}, // helperLocal is same-file
		"util/merge.js":  {"merge": {2}},
		"util/shared.js": {"sharedHelper": {3}},
	}
	callerIDs = []int64{1, 1, 1}
	meta = map[int64]NodeMeta{
		1: {Label: "Function", Name: "run", File: "app/main.js"},
		2: {Label: "Function", Name: "merge", File: "util/merge.js"},
		3: {Label: "Function", Name: "sharedHelper", File: "util/shared.js"},
		4: {Label: "Function", Name: "helperLocal", File: "app/main.js"},
	}
	return
}

func pyFixture() (calls []parser.CallRef, nodeIDs map[string][]int64,
	fileNodeIDs map[string]map[string][]int64, callerIDs []int64,
	imports []parser.ImportRef, fm map[string][]string, meta map[int64]NodeMeta) {

	files := []string{"app/caller.py", "lib/local.py"}
	langs := []string{"python", "python"}
	fm = BuildFileMap(files, langs)

	imports = []parser.ImportRef{
		{ImportedName: "y", ModulePath: "extpkg", File: "app/caller.py", Line: 1},
	}
	calls = []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "y", CalleeQualified: "y", Line: 5, File: "app/caller.py"},
		{CallerNodeIdx: 0, CalleeName: "z", CalleeQualified: "z", Line: 6, File: "app/caller.py"},
	}
	nodeIDs = map[string][]int64{"handler": {1}, "y": {2}, "z": {3}}
	fileNodeIDs = map[string]map[string][]int64{
		"app/caller.py": {"handler": {1}},
		"lib/local.py":  {"y": {2}, "z": {3}},
	}
	callerIDs = []int64{1, 1}
	meta = map[int64]NodeMeta{
		1: {Label: "Function", Name: "handler", File: "app/caller.py"},
		2: {Label: "Function", Name: "y", File: "lib/local.py"},
		3: {Label: "Function", Name: "z", File: "lib/local.py"},
	}
	return
}

func TestResolve_B1NegEv_JS_ExternalImportDropped(t *testing.T) {
	calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := jsFixture()

	// OFF: the wrong name_match onto the local merge(2) IS minted (today's behavior).
	os.Unsetenv("GT_NEG_EVIDENCE")
	off := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if r, ok := hasEdge(off, 1, 2); !ok || r.Method != "name_match" {
		t.Fatalf("OFF: expected the (wrong) name_match onto external-bound merge(2); got %+v ok=%v", r, ok)
	}
	if _, ok := hasEdge(off, 1, 3); !ok {
		t.Fatalf("OFF: the not-imported sharedHelper(3) must resolve (name_match)")
	}
	if r, ok := hasEdge(off, 1, 4); !ok || r.Method != "same_file" {
		t.Fatalf("OFF: same-file helperLocal(4) must resolve same_file; got %+v ok=%v", r, ok)
	}

	// ON: the external-bound merge is DROPPED; the not-imported + same-file calls survive.
	os.Setenv("GT_NEG_EVIDENCE", "1")
	defer os.Unsetenv("GT_NEG_EVIDENCE")
	on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if r, ok := hasEdge(on, 1, 2); ok {
		t.Errorf("ON: external-bound merge(2) must be DROPPED, but got edge %+v", r)
	}
	if _, ok := hasEdge(on, 1, 3); !ok {
		t.Errorf("ON: over-drop! the NOT-imported sharedHelper(3) must be UNAFFECTED")
	}
	if r, ok := hasEdge(on, 1, 4); !ok || r.Method != "same_file" {
		t.Errorf("ON: over-drop! same-file helperLocal(4) must be UNAFFECTED; got %+v ok=%v", r, ok)
	}
}

func TestResolve_B1NegEv_Python_ExternalImportDropped(t *testing.T) {
	calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := pyFixture()

	os.Unsetenv("GT_NEG_EVIDENCE")
	off := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if r, ok := hasEdge(off, 1, 2); !ok || r.Method != "name_match" {
		t.Fatalf("OFF: expected the (wrong) name_match onto external-bound y(2); got %+v ok=%v", r, ok)
	}
	if _, ok := hasEdge(off, 1, 3); !ok {
		t.Fatalf("OFF: the not-imported z(3) must resolve (name_match)")
	}

	os.Setenv("GT_NEG_EVIDENCE", "1")
	defer os.Unsetenv("GT_NEG_EVIDENCE")
	on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if r, ok := hasEdge(on, 1, 2); ok {
		t.Errorf("ON: external-bound y(2) must be DROPPED, but got edge %+v", r)
	}
	if _, ok := hasEdge(on, 1, 3); !ok {
		t.Errorf("ON: over-drop! the NOT-imported z(3) must be UNAFFECTED")
	}
}

// Byte-identical-off: the OFF edge set is EXACTLY equal across two indexings and is the
// full pre-change set (nothing dropped). Drives the REAL Resolve twice with the flag off.
func TestResolve_B1NegEv_ByteIdenticalOff_Determinism(t *testing.T) {
	os.Unsetenv("GT_NEG_EVIDENCE")
	for _, fx := range []func() ([]parser.CallRef, map[string][]int64, map[string]map[string][]int64, []int64, []parser.ImportRef, map[string][]string, map[int64]NodeMeta){jsFixture, pyFixture} {
		calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := fx()
		a := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
		b := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
		if len(a) != len(b) {
			t.Fatalf("OFF determinism: edge count differs %d vs %d", len(a), len(b))
		}
		for i := range a {
			if a[i] != b[i] {
				t.Fatalf("OFF determinism: edge %d differs %+v vs %+v", i, a[i], b[i])
			}
		}
	}
}

// ON determinism: two indexings with the flag on are byte-identical (the drop is
// deterministic; externalBoundNames is a set built order-independently).
func TestResolve_B1NegEv_ON_Determinism(t *testing.T) {
	os.Setenv("GT_NEG_EVIDENCE", "1")
	defer os.Unsetenv("GT_NEG_EVIDENCE")
	calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := jsFixture()
	a := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
	b := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
	if len(a) != len(b) {
		t.Fatalf("ON determinism: edge count differs %d vs %d", len(a), len(b))
	}
	for i := range a {
		if a[i] != b[i] {
			t.Fatalf("ON determinism: edge %d differs %+v vs %+v", i, a[i], b[i])
		}
	}
}

// MUTATION companion (F6). The correct guard drops ONLY a name that is import-BOUND to a
// provably-external module (present in externalBoundNames). The load-bearing half is the
// import-binding requirement (negEvidenceRequireImportBinding, default true). This test
// FIRST proves the production guard leaves a NOT-imported cross-file name_match
// (sharedHelper) intact ON; THEN flips the real package lever to neuter the import-binding
// requirement (degrading to "drop any name not resolved via importIndex") and asserts
// sharedHelper is now WRONGLY dropped — proving the requirement is load-bearing. The prior
// version asserted only survival, which held regardless of any guard (sharedHelper is never
// in the external-bound set), so it neutered nothing.
func TestResolve_B1NegEv_Mutation_NotImportedNameSurvivesON(t *testing.T) {
	os.Setenv("GT_NEG_EVIDENCE", "1")
	defer os.Unsetenv("GT_NEG_EVIDENCE")

	// Production guard: sharedHelper(3) is NOT imported → not external-bound → survives.
	calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := jsFixture()
	on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if _, ok := hasEdge(on, 1, 3); !ok {
		t.Fatalf("PROD GUARD: sharedHelper(3) is NOT imported and must survive ON")
	}

	// NEUTER the import-binding requirement and assert the over-drop appears.
	saved := negEvidenceRequireImportBinding
	negEvidenceRequireImportBinding = false
	defer func() { negEvidenceRequireImportBinding = saved }()
	calls2, nodeIDs2, fileNodeIDs2, callerIDs2, imports2, fm2, meta2 := jsFixture()
	neutered := Resolve(calls2, nodeIDs2, fileNodeIDs2, callerIDs2, imports2, fm2, meta2)
	if _, ok := hasEdge(neutered, 1, 3); ok {
		t.Errorf("MUTATION GUARD: with the import-binding requirement neutered, the NOT-imported " +
			"sharedHelper(3) must be WRONGLY dropped — its survival proves the requirement was not load-bearing")
	}
	// The same-file helperLocal(4) must STILL survive even neutered (it has a local def, so
	// the drop's local-def guard keeps it) — the over-drop is scoped to genuinely-absent names.
	if r, ok := hasEdge(neutered, 1, 4); !ok || r.Method != "same_file" {
		t.Errorf("MUTATION: same-file helperLocal(4) must survive even neutered; got %+v ok=%v", r, ok)
	}
}

// F2 (B1 over-drop) RED→GREEN. A genuinely-INTERNAL import whose path form resolveModulePath
// cannot map (`from a.b.compute_util import compute`, where the project DOES have
// helpers/compute_util.py) must NOT be dropped: an importIndex miss is not proof of
// externality. Pre-fix the bare compute() call was dropped (importIndex miss ⇒ marked
// external-bound); post-fix moduleProvablyExternal sees a project file segment
// (`compute_util`) → uncertain → keeps the edge (name_match).
func internalUnresolvedFixture() (calls []parser.CallRef, nodeIDs map[string][]int64,
	fileNodeIDs map[string]map[string][]int64, callerIDs []int64,
	imports []parser.ImportRef, fm map[string][]string, meta map[int64]NodeMeta) {

	files := []string{"app/caller.py", "helpers/compute_util.py"}
	langs := []string{"python", "python"}
	fm = BuildFileMap(files, langs)
	// A dotted absolute import whose form resolveModulePath cannot map to the file layout,
	// yet clearly refers to the internal helpers/compute_util.py (leaf `compute_util`).
	imports = []parser.ImportRef{
		{ImportedName: "compute", ModulePath: "a.b.compute_util", File: "app/caller.py", Line: 1},
	}
	calls = []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "compute", CalleeQualified: "compute", Line: 5, File: "app/caller.py"},
	}
	nodeIDs = map[string][]int64{"handler": {1}, "compute": {2}}
	fileNodeIDs = map[string]map[string][]int64{
		"app/caller.py":           {"handler": {1}},
		"helpers/compute_util.py": {"compute": {2}},
	}
	callerIDs = []int64{1}
	meta = map[int64]NodeMeta{
		1: {Label: "Function", Name: "handler", File: "app/caller.py"},
		2: {Label: "Function", Name: "compute", File: "helpers/compute_util.py"},
	}
	return
}

func TestResolve_B1NegEv_InternalUnresolvedImportNotDropped(t *testing.T) {
	calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := internalUnresolvedFixture()

	// Sanity (OFF): the internal compute() resolves (name_match) — nothing dropped.
	os.Unsetenv("GT_NEG_EVIDENCE")
	off := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if _, ok := hasEdge(off, 1, 2); !ok {
		t.Fatalf("OFF: internal compute(2) must resolve; got no edge (all=%+v)", off)
	}

	// ON: the genuinely-internal (but resolveModulePath-unmapped) import must NOT be dropped.
	os.Setenv("GT_NEG_EVIDENCE", "1")
	defer os.Unsetenv("GT_NEG_EVIDENCE")
	on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if _, ok := hasEdge(on, 1, 2); !ok {
		t.Errorf("F2 OVER-DROP: the internal compute(2) call was wrongly dropped — an importIndex " +
			"miss is not proof of externality; moduleProvablyExternal must see the project segment and keep it")
	}
}

// F2 guard: moduleProvablyExternal still marks a GENUINE third-party module external so the
// legitimate external drop (lodash/extpkg) is untouched, and treats a RELATIVE import as
// uncertain (never provably external).
func TestModuleProvablyExternal_Classification(t *testing.T) {
	fm := BuildFileMap([]string{"app/main.js", "util/merge.js"}, []string{"javascript", "javascript"})
	cases := []struct {
		mod  string
		want bool
	}{
		{"lodash", true},          // third-party leaf, no project file → external
		{"extpkg", true},          // third-party → external
		{"merge", false},          // names a project file (util/merge.js stem) → uncertain
		{"util/merge", false},     // a project module path → uncertain
		{"./local", false},        // relative → never provably external
		{"../x", false},           // relative → never provably external
		{"", false},               // unknown → uncertain
		{"react.Component", true}, // no segment names a project file → external
	}
	segs := buildProjectPathSegments(fm)
	for _, c := range cases {
		if got := moduleProvablyExternal(c.mod, fm, segs); got != c.want {
			t.Errorf("moduleProvablyExternal(%q) = %v; want %v", c.mod, got, c.want)
		}
	}
}

// F9 (monorepo/workspace over-drop) RED→GREEN. A pnpm/yarn scoped-package import
// (`import {helper} from '@org/utils'`) inside the SAME repo maps to packages/utils/…,
// whose "utils" DIRECTORY is a path segment of indexed files but NOT a fileMap KEY (JS
// registration keys stems + index-dirs only). Pre-fix, moduleProvablyExternal's key-only
// segment check declared @org/utils "provably external" and B1 dropped the helper() call —
// a genuinely-internal edge lost to a false externality proof. Post-fix the project
// path-segment set (buildProjectPathSegments) marks it UNCERTAIN → the call survives as
// today's name_match. The genuine external drop (lodash) must STAY dropped (control).
func TestResolve_B1NegEv_WorkspaceScopedImportNotDropped(t *testing.T) {
	files := []string{"apps/web/main.ts", "packages/utils/lib.ts"}
	langs := []string{"typescript", "typescript"}
	fm := BuildFileMap(files, langs)

	imports := []parser.ImportRef{
		// Workspace-internal scoped package: resolveModulePath cannot map it, but
		// "utils" IS a project directory segment → NOT provably external.
		{ImportedName: "helper", ModulePath: "@org/utils", File: "apps/web/main.ts", Line: 1},
		// Genuine third-party control: no segment names anything in the project.
		{ImportedName: "merge", ModulePath: "lodash", File: "apps/web/main.ts", Line: 2},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "helper", CalleeQualified: "helper", Line: 5, File: "apps/web/main.ts"},
		{CallerNodeIdx: 0, CalleeName: "merge", CalleeQualified: "merge", Line: 6, File: "apps/web/main.ts"},
	}
	nodeIDs := map[string][]int64{"run": {1}, "helper": {2}, "merge": {3}}
	fileNodeIDs := map[string]map[string][]int64{
		"apps/web/main.ts":      {"run": {1}},
		"packages/utils/lib.ts": {"helper": {2}, "merge": {3}},
	}
	callerIDs := []int64{1, 1}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "run", File: "apps/web/main.ts"},
		2: {Label: "Function", Name: "helper", File: "packages/utils/lib.ts"},
		3: {Label: "Function", Name: "merge", File: "packages/utils/lib.ts"},
	}

	os.Setenv("GT_NEG_EVIDENCE", "1")
	defer os.Unsetenv("GT_NEG_EVIDENCE")
	on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if _, ok := hasEdge(on, 1, 2); !ok {
		t.Errorf("F9 OVER-DROP: workspace-internal @org/utils helper(2) was wrongly dropped — " +
			"\"utils\" is a project path segment, so the module is NOT provably external")
	}
	if r, ok := hasEdge(on, 1, 3); ok {
		t.Errorf("F9 CONTROL: genuinely-external lodash merge(3) must STAY dropped; got %+v", r)
	}
}
