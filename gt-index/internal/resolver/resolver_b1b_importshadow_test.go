package resolver

import (
	"os"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ---------------------------------------------------------------------------
// B1b IMPORT-CONSISTENCY (GT_NEG_EVIDENCE, default off) — B1's missing half.
//
// Reproduces the arktype funnel (run 28699793199, Row-3 nm>det): a TEST file does
// `import { type } from "../index"`, which RESOLVES to an indexed file (ark/type/index.ts)
// — so B1's external-drop correctly ABSTAINS (the import is internal). But the flagship
// `type` export is a generic const the indexer cannot node-ify, so index.ts has NO `type`
// node; the only same-named node lives in a DIFFERENT file (attest/chainable.ts). Today
// the resolver mints a cross-file name_match onto that unrelated file — a WRONG fact the
// import index just disproved. B1b drops it (correct-or-quiet).
//
// Guardrails proven here: byte-identical-off, no over-drop of a LEGITIMATE resolving
// import (helper, whose def IS in the imported file → kept), framework-global immunity
// (expect(), NOT import-bound by name → untouched — the katex case B1b must not touch),
// determinism (both modes), TS + JS.
// ---------------------------------------------------------------------------

// importShadowFixture models: ark/type/__tests__/roundtrip.test.ts imports {type} from
// "../index" (→ ark/type/index.ts, which has NO `type` node) and {helper} from "../util"
// (→ ark/type/util.ts, which HAS a helper node). It calls type(), helper() and a bare,
// NON-imported framework global expect() (whose coincidental node lives in attest/chainable.ts).
func importShadowFixture(lang string) (calls []parser.CallRef, nodeIDs map[string][]int64,
	fileNodeIDs map[string]map[string][]int64, callerIDs []int64,
	imports []parser.ImportRef, fm map[string][]string, meta map[int64]NodeMeta) {

	ext := ".ts"
	if lang == "javascript" {
		ext = ".js"
	}
	testFile := "ark/type/__tests__/roundtrip.test" + ext
	indexFile := "ark/type/index" + ext     // import target of `type` — has NO type node
	chainFile := "ark/attest/chainable" + ext // where the coincidental type/expect nodes live
	utilFile := "ark/type/util" + ext         // import target of `helper` — HAS a helper node

	files := []string{testFile, indexFile, chainFile, utilFile}
	langs := []string{lang, lang, lang, lang}
	fm = BuildFileMap(files, langs)

	imports = []parser.ImportRef{
		// `type` is import-bound to ../index (RESOLVES to indexFile — internal, so B1 abstains).
		{ImportedName: "type", ModulePath: "../index", File: testFile, Line: 1},
		// `helper` is import-bound to ../util (RESOLVES to utilFile, which DOES define helper).
		{ImportedName: "helper", ModulePath: "../util", File: testFile, Line: 2},
		// NOTE: `expect` is NOT imported — a jest-injected global (the katex shape).
	}
	calls = []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "type", CalleeQualified: "type", Line: 5, File: testFile},
		{CallerNodeIdx: 0, CalleeName: "helper", CalleeQualified: "helper", Line: 6, File: testFile},
		{CallerNodeIdx: 0, CalleeName: "expect", CalleeQualified: "expect", Line: 7, File: testFile},
	}
	nodeIDs = map[string][]int64{
		"roundtrip": {1}, "type": {2}, "helper": {3}, "expect": {4}, "internalMake": {5},
	}
	fileNodeIDs = map[string]map[string][]int64{
		testFile:  {"roundtrip": {1}},
		indexFile: {"internalMake": {5}}, // index.ts is indexed but has NO `type` node
		chainFile: {"type": {2}, "expect": {4}},
		utilFile:  {"helper": {3}},
	}
	callerIDs = []int64{1, 1, 1}
	meta = map[int64]NodeMeta{
		1: {Label: "Function", Name: "roundtrip", File: testFile},
		2: {Label: "Method", Name: "type", File: chainFile},
		3: {Label: "Function", Name: "helper", File: utilFile},
		4: {Label: "Method", Name: "expect", File: chainFile},
		5: {Label: "Function", Name: "internalMake", File: indexFile},
	}
	return
}

func runImportShadowCase(t *testing.T, lang string) {
	t.Helper()
	calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := importShadowFixture(lang)

	// Precondition: the import of `type` MUST resolve to the index file (else the fixture
	// is not modeling the funnel — B1b only fires on an import-bound-but-target-absent name).
	idx := buildImportIndex(imports, fm)
	if bound := idx[calls[0].File]["type"]; len(bound) == 0 {
		t.Fatalf("[%s] fixture invalid: `type` did not import-resolve to any file (idx=%v)", lang, idx)
	}

	// OFF: today's behavior — the WRONG cross-file name_match onto attest/chainable type(2)
	// IS minted (a coincidental same-name the import disproves); the legit helper(3) and the
	// framework-global expect(4) also resolve.
	os.Unsetenv("GT_NEG_EVIDENCE")
	off := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if _, ok := hasEdge(off, 1, 2); !ok {
		t.Fatalf("[%s] OFF: expected the (wrong) name_match onto import-shadowed type(2); got none (all=%+v)", lang, off)
	}
	if _, ok := hasEdge(off, 1, 3); !ok {
		t.Fatalf("[%s] OFF: legit import helper(3) must resolve", lang)
	}
	if _, ok := hasEdge(off, 1, 4); !ok {
		t.Fatalf("[%s] OFF: framework-global expect(4) must resolve (name_match)", lang)
	}

	// ON: B1b DROPS the import-shadowed type(2) edge; helper(3) and expect(4) SURVIVE.
	os.Setenv("GT_NEG_EVIDENCE", "1")
	defer os.Unsetenv("GT_NEG_EVIDENCE")
	on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if r, ok := hasEdge(on, 1, 2); ok {
		t.Errorf("[%s] ON: import-shadowed type(2) must be DROPPED (import resolves to index%s "+
			"which has no `type` node); got edge %+v", lang, "", r)
	}
	if _, ok := hasEdge(on, 1, 3); !ok {
		t.Errorf("[%s] ON: OVER-DROP! helper(3) is import-bound to util where it IS defined — must survive", lang)
	}
	if _, ok := hasEdge(on, 1, 4); !ok {
		t.Errorf("[%s] ON: OVER-DROP! framework-global expect(4) is NOT import-bound by name — must be untouched", lang)
	}
}

func TestResolve_B1b_ImportShadow_TS(t *testing.T) { runImportShadowCase(t, "typescript") }
func TestResolve_B1b_ImportShadow_JS(t *testing.T) { runImportShadowCase(t, "javascript") }

// Byte-identical-off + determinism (both modes): the drop is deterministic and the flag-off
// edge set is stable across two indexings.
func TestResolve_B1b_ImportShadow_Determinism(t *testing.T) {
	for _, lang := range []string{"typescript", "javascript"} {
		for _, mode := range []string{"off", "on"} {
			if mode == "on" {
				os.Setenv("GT_NEG_EVIDENCE", "1")
			} else {
				os.Unsetenv("GT_NEG_EVIDENCE")
			}
			calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := importShadowFixture(lang)
			a := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
			b := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
			if len(a) != len(b) {
				os.Unsetenv("GT_NEG_EVIDENCE")
				t.Fatalf("[%s/%s] determinism: edge count %d vs %d", lang, mode, len(a), len(b))
			}
			for i := range a {
				if a[i] != b[i] {
					os.Unsetenv("GT_NEG_EVIDENCE")
					t.Fatalf("[%s/%s] determinism: edge %d differs %+v vs %+v", lang, mode, i, a[i], b[i])
				}
			}
		}
	}
	os.Unsetenv("GT_NEG_EVIDENCE")
}

// MUTATION companion: prove the "candidate IS in the imported file-set → keep" half is
// load-bearing. If helper's ONLY node is moved OUT of the imported file (util) into an
// unrelated file, ON must now DROP it too (import-shadowed) — its survival in the main
// test therefore proves the in-F* check, not a no-op.
func TestResolve_B1b_ImportShadow_Mutation_InFileSetGuardLoadBearing(t *testing.T) {
	os.Setenv("GT_NEG_EVIDENCE", "1")
	defer os.Unsetenv("GT_NEG_EVIDENCE")

	calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := importShadowFixture("typescript")
	// Move helper's def from util.ts (the imported file) to an unrelated file: now the import
	// of helper from ../util resolves to a file with NO helper node, and helper(3) lives elsewhere.
	fileNodeIDs["ark/type/util.ts"] = map[string][]int64{"unrelatedUtil": {6}}
	fileNodeIDs["ark/other/misc.ts"] = map[string][]int64{"helper": {3}}
	meta[3] = NodeMeta{Label: "Function", Name: "helper", File: "ark/other/misc.ts"}
	meta[6] = NodeMeta{Label: "Function", Name: "unrelatedUtil", File: "ark/type/util.ts"}
	nodeIDs["unrelatedUtil"] = []int64{6}
	// Re-register the file map so ark/other/misc.ts exists for path resolution.
	fm = BuildFileMap(
		[]string{"ark/type/__tests__/roundtrip.test.ts", "ark/type/index.ts",
			"ark/attest/chainable.ts", "ark/type/util.ts", "ark/other/misc.ts"},
		[]string{"typescript", "typescript", "typescript", "typescript", "typescript"},
	)

	on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if _, ok := hasEdge(on, 1, 3); ok {
		t.Errorf("MUTATION: helper(3) moved OUT of the imported file util.ts must now be DROPPED " +
			"(import-shadowed) — its survival in the main test proves the in-file-set check is load-bearing")
	}
}

// INCREMENTAL-PATH SOUNDNESS (DEMOTE, not drop, not abstain): on the `-file` reindex path
// ChainReExports never folds re-export SOURCES into fileMap, so F* is a bare direct-module
// resolution and a legitimately re-exported def looks "absent" → a DROP would delete a true
// edge. The caller marks reExportGraphIncomplete=true there; B1b must then DEMOTE the
// shadowed candidate to sub-floor name_match (conf 0.2) — NOT drop it (the full-path action)
// and NOT plain-abstain (which lets Strategy 1.9 re-mint it at verified_unique 0.95 CERTIFIED
// when provenance holds — Fable Finding 1). Proves: the edge SURVIVES, but sub-floor and
// honestly labelled, so no fact reaches the agent; helper(3)/expect(4) untouched.
func TestResolve_B1b_ImportShadow_IncrementalReExportGraph_Demotes(t *testing.T) {
	os.Setenv("GT_NEG_EVIDENCE", "1")
	defer os.Unsetenv("GT_NEG_EVIDENCE")
	// Package-level state — restore the default (complete=DROP-active) so it cannot leak into
	// the other B1b/resolver tests that run after this one in the same process.
	SetReExportGraphIncomplete(true)
	defer SetReExportGraphIncomplete(false)

	calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := importShadowFixture("typescript")

	// With the re-export graph marked incomplete (the incremental path), the SAME edge the
	// full-path ON case DROPS (type(2), import-shadowed) must SURVIVE but be DEMOTED to
	// name_match conf 0.2 (below the 0.5 consumer floor → filtered), never CERTIFIED.
	on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	r, ok := hasEdge(on, 1, 2)
	if !ok {
		t.Errorf("INCREMENTAL: reExportGraphIncomplete=true must DEMOTE (not drop) type(2) — " +
			"the re-export source was never folded, so absence is not proof; the edge must survive")
	} else {
		if r.Confidence > 0.2 {
			t.Errorf("INCREMENTAL: demoted type(2) must be sub-floor (conf<=0.2), got %.3f "+
				"(method=%s) — an abstain that let 1.9 re-certify would be the Finding-1 bug",
				r.Confidence, r.Method)
		}
		if r.Method != "name_match" {
			t.Errorf("INCREMENTAL: demoted type(2) must be method=name_match (a guess, not a "+
				"fact), got %q", r.Method)
		}
	}
	if _, ok := hasEdge(on, 1, 3); !ok {
		t.Errorf("INCREMENTAL: legit import helper(3) must survive")
	}
	if _, ok := hasEdge(on, 1, 4); !ok {
		t.Errorf("INCREMENTAL: framework-global expect(4) is not import-bound → must be untouched")
	}
}

// SAME-DIR DEMOTE (Fable Finding 1 reproduction): the shadowed candidate lives in the SAME
// DIRECTORY as the caller, so Strategy 1.9's provenance gate (sameDirFile) ACCEPTS it and would
// mint verified_unique 0.95 CERTIFIED. This is precisely the launder a plain abstain on the
// incremental path re-opens. The test proves three things: (CONTROL) the fixture DOES certify
// without B1b; (FULL) B1b drops it; (INCREMENTAL) B1b DEMOTES to name_match <=0.2, never
// re-certifies. Distinguishes demote from abstain (the earlier different-dir fixture could not,
// as its provenance failed anyway).
func TestResolve_B1b_ImportShadow_IncrementalSameDir_DemotesNotCertifies(t *testing.T) {
	callerFile := "pkg/caller.ts"
	indexFile := "pkg/index.ts" // import target of `flag` — has NO flag node
	otherFile := "pkg/other.ts" // SAME DIR as caller — has the coincidental flag node
	fm := BuildFileMap(
		[]string{callerFile, indexFile, otherFile},
		[]string{"typescript", "typescript", "typescript"},
	)
	imports := []parser.ImportRef{
		{ImportedName: "flag", ModulePath: "./index", File: callerFile, Line: 1},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "flag", CalleeQualified: "flag", Line: 5, File: callerFile},
	}
	nodeIDs := map[string][]int64{"useFlag": {1}, "flag": {2}, "internalMake": {5}}
	fileNodeIDs := map[string]map[string][]int64{
		callerFile: {"useFlag": {1}},
		indexFile:  {"internalMake": {5}},
		otherFile:  {"flag": {2}},
	}
	callerIDs := []int64{1}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "useFlag", File: callerFile},
		2: {Label: "Function", Name: "flag", File: otherFile},
		5: {Label: "Function", Name: "internalMake", File: indexFile},
	}
	if bound := buildImportIndex(imports, fm)[callerFile]["flag"]; len(bound) == 0 {
		t.Fatalf("fixture invalid: flag did not import-resolve to index")
	}
	defer os.Unsetenv("GT_NEG_EVIDENCE")
	defer SetReExportGraphIncomplete(false)

	// CONTROL — NEG OFF: same-dir provenance → 1.9 mints verified_unique (the launder this
	// guard exists to stop). Proves the fixture certifies without B1b.
	os.Unsetenv("GT_NEG_EVIDENCE")
	SetReExportGraphIncomplete(false)
	off := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if r, ok := hasEdge(off, 1, 2); !ok || r.Method != "verified_unique" {
		t.Fatalf("CONTROL: NEG off must mint verified_unique via same-dir provenance; ok=%v edge=%+v", ok, r)
	}

	// FULL-INDEX, NEG ON: F* complete → B1b DROPS.
	os.Setenv("GT_NEG_EVIDENCE", "1")
	SetReExportGraphIncomplete(false)
	full := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if _, ok := hasEdge(full, 1, 2); ok {
		t.Errorf("FULL: B1b must DROP the same-dir import-shadowed flag(2)")
	}

	// INCREMENTAL, NEG ON: F* incomplete → B1b DEMOTES to name_match <=0.2, NOT verified_unique.
	SetReExportGraphIncomplete(true)
	inc := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	r, ok := hasEdge(inc, 1, 2)
	if !ok {
		t.Fatalf("INCREMENTAL: B1b must DEMOTE (keep) the shadowed flag(2), not drop it")
	}
	if r.Method == "verified_unique" || r.Confidence > 0.2 {
		t.Errorf("INCREMENTAL: Finding-1 regression — same-dir shadowed flag(2) re-certified as "+
			"%s conf %.3f; must be demoted to name_match <=0.2", r.Method, r.Confidence)
	}
}
