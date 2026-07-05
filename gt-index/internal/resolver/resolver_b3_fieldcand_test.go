package resolver

import (
	"os"
	"sort"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ---------------------------------------------------------------------------
// B3 — field-based CANDIDATE SETS for the receiver-unproven residual
// (GT_FIELD_CANDIDATES, default off).
//
// For `obj.method()` where NO typing rung fires (dynamic/untyped receiver), the tail
// mints ONE arbitrary name_match. ACG field-based (Feldthaus/Sridharan/Tip, ICSE 2013):
// the honest answer is the CANDIDATE SET. Under the flag the tail instead emits
//
//	set = {nodes named method} ∩ import-reachable(caller) ∩ is_exported
//
// as a NEW categorical resolution_method "field_based" with candidate_count=K, tier
// capped at CANDIDATE — never a single-target CERTIFIED fact, never name_match laundered
// as fact. Empty intersection → drop (correct-or-quiet). K==1 with the 1.9 provenance
// gate promotes ONE notch (0.5→0.6), still strictly below any verified tier.
//
// Guardrails proven here: OFF mints 1 arbitrary name_match (byte-identical); ON emits
// EXACTLY the reachable+exported set (completeness) at CANDIDATE tier, never CERTIFIED;
// K==1 promotes one notch but stays < 0.9; empty-intersection drops; ≥2 languages
// (JS + Python). The MUTATION companion neuters the import-reachability filter and
// asserts the set then wrongly admits the UNREACHABLE candidate.
// ---------------------------------------------------------------------------

// fieldCandFixture builds a repo where `obj.handle()` (untyped receiver) has four project
// defs of the FREE FUNCTION handle:
//
//	id 2  svc/a  exported, IMPORT-reachable
//	id 3  svc/b  exported, IMPORT-reachable
//	id 4  svc/c  exported, NOT reachable   (never imported)
//	id 5  svc/d  NOT exported, reachable   (imported)
//
// so the honest ON set is exactly {2,3} — c excluded by reachability, d by the export
// filter (both filters load-bearing). `imported` selects which module basenames the
// caller imports (drives the K==1 and empty-intersection variants). handle is a free
// function (no class), so no self/impl/unique/type_flow rung can fire → the call reaches
// the qualified-unresolved multi-candidate tail. lang ∈ {javascript, python}.
func fieldCandFixture(lang string, imported []string) (calls []parser.CallRef, nodeIDs map[string][]int64,
	fileNodeIDs map[string]map[string][]int64, callerIDs []int64,
	imports []parser.ImportRef, fm map[string][]string, meta map[int64]NodeMeta) {

	ext := ".js"
	if lang == "python" {
		ext = ".py"
	}
	mainFile := "app/main" + ext
	fa := "svc/a" + ext
	fb := "svc/b" + ext
	fc := "svc/c" + ext
	fd := "svc/d" + ext
	files := []string{mainFile, fa, fb, fc, fd}
	langs := []string{lang, lang, lang, lang, lang}
	fm = BuildFileMap(files, langs)

	// module path the caller uses to import a basename: JS "svc/a", Python "svc.a".
	modOf := func(base string) string {
		if lang == "python" {
			return "svc." + base
		}
		return "svc/" + base
	}
	moduleFileByBase := map[string]string{"a": fa, "b": fb, "c": fc, "d": fd}
	for _, base := range imported {
		if _, ok := moduleFileByBase[base]; !ok {
			continue
		}
		imports = append(imports, parser.ImportRef{
			ImportedName: "helper_" + base,
			ModulePath:   modOf(base),
			File:         mainFile,
			Line:         1,
		})
	}

	nodeIDs = map[string][]int64{
		"run":    {1},
		"handle": {2, 3, 4, 5},
	}
	fileNodeIDs = map[string]map[string][]int64{
		mainFile: {"run": {1}},
		fa:       {"handle": {2}},
		fb:       {"handle": {3}},
		fc:       {"handle": {4}},
		fd:       {"handle": {5}},
	}
	meta = map[int64]NodeMeta{
		1: {Label: "Function", File: mainFile, Name: "run", StartLine: 1},
		2: {Label: "Function", File: fa, Name: "handle", IsExported: true, StartLine: 10},
		3: {Label: "Function", File: fb, Name: "handle", IsExported: true, StartLine: 10},
		4: {Label: "Function", File: fc, Name: "handle", IsExported: true, StartLine: 10},
		5: {Label: "Function", File: fd, Name: "handle", IsExported: false, StartLine: 10},
	}
	calls = []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "handle", CalleeQualified: "obj.handle", Line: 5, File: mainFile},
	}
	callerIDs = []int64{1}
	return
}

// clearNonMetaGlobals nils every global index so ONLY the name-based tail (and B3 over it)
// is in play — no assignment/param/field/return-shape resolution can pre-empt the call.
func clearNonMetaGlobals() {
	SetAssignmentIndex(nil)
	SetParamTypeIndex(nil)
	SetFieldTypeIndex(nil)
	SetReturnShapeIndex(nil)
	SetInheritanceMap(nil)
}

// fieldBasedTargets returns the sorted set of targets the caller (id 1) resolves to via
// method "field_based", plus the first such edge (for tier/conf assertions).
func fieldBasedTargets(rcs []ResolvedCall) ([]int64, ResolvedCall) {
	var tgts []int64
	var first ResolvedCall
	for _, r := range rcs {
		if r.SourceNodeID == 1 && r.Method == "field_based" {
			tgts = append(tgts, r.TargetNodeID)
			if first.SourceNodeID == 0 {
				first = r
			}
		}
	}
	sort.Slice(tgts, func(i, j int) bool { return tgts[i] < tgts[j] })
	return tgts, first
}

func nameMatchCount(rcs []ResolvedCall, src int64) int {
	n := 0
	for _, r := range rcs {
		if r.SourceNodeID == src && r.Method == "name_match" {
			n++
		}
	}
	return n
}

func TestResolve_FieldCandidates_SetCompleteness(t *testing.T) {
	for _, lang := range []string{"javascript", "python"} {
		t.Run(lang, func(t *testing.T) {
			clearNonMetaGlobals()
			// caller imports a, b, d (NOT c). Reachable = {2,3,5}; exported = {2,3,4}.
			calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := fieldCandFixture(lang, []string{"a", "b", "d"})

			// OFF: exactly ONE arbitrary name_match, no field_based edge.
			os.Unsetenv("GT_FIELD_CANDIDATES")
			off := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
			if got := nameMatchCount(off, 1); got != 1 {
				t.Fatalf("OFF: expected exactly 1 arbitrary name_match for obj.handle(); got %d (all=%+v)", got, off)
			}
			if tg, _ := fieldBasedTargets(off); len(tg) != 0 {
				t.Fatalf("OFF: no field_based edge must be emitted; got %v", tg)
			}

			// ON: EXACTLY the reachable+exported set {2,3}. c(4) excluded by reachability,
			// d(5) excluded by the export filter — both filters load-bearing.
			os.Setenv("GT_FIELD_CANDIDATES", "1")
			defer os.Unsetenv("GT_FIELD_CANDIDATES")
			on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
			tg, first := fieldBasedTargets(on)
			if len(tg) != 2 || tg[0] != 2 || tg[1] != 3 {
				t.Fatalf("ON: field_based set must be EXACTLY {2,3} (reachable ∩ exported); got %v (all=%+v)", tg, on)
			}
			if nameMatchCount(on, 1) != 0 {
				t.Errorf("ON: the field_based set REPLACES the name_match guess; a name_match must not co-exist")
			}
			if first.CandidateCount != 2 {
				t.Errorf("ON: each field_based edge must carry candidate_count=K=2; got %d", first.CandidateCount)
			}
			if first.TrustTier != "CANDIDATE" {
				t.Errorf("ON: K>=2 field_based tier must be CANDIDATE (never CERTIFIED); got %q (conf=%v)", first.TrustTier, first.Confidence)
			}
			if first.Confidence >= 0.9 {
				t.Errorf("ON: field_based must be strictly below CERTIFIED (0.9); got conf=%v", first.Confidence)
			}
			// Fable RS1 gradient, low end: K==2 stays at the 0.5 CANDIDATE floor.
			if first.Confidence != 0.5 {
				t.Errorf("ON: K==2 field_based must be conf 0.5 (ambiguity-gradient floor); got %v", first.Confidence)
			}
			if first.EvidenceType != "field_based" {
				t.Errorf("ON: K>=2 evidence must be field_based; got %q", first.EvidenceType)
			}
		})
	}
}

// Fable RS1 (ambiguity gradient, RED→GREEN): field_based minted a FLAT 0.5 for ANY K, so a
// K=3 fan-out entered the closure/fact floor (0.5) identically to a K=2 one. With the
// gradient K≤5→0.4 (below the 0.5 traversal floor), the wide-but-not-huge guess is still
// STORED for the LSP residual but no longer poses as reachable fact. Importing {a,b,c} makes
// all three exported defs {2,3,4} reachable → K=3 → conf 0.4. RED before the gradient
// (flat 0.5); GREEN after.
func TestResolve_FieldCandidates_AmbiguityGradient_K3IsBelowFloor(t *testing.T) {
	for _, lang := range []string{"javascript", "python"} {
		t.Run(lang, func(t *testing.T) {
			clearNonMetaGlobals()
			calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := fieldCandFixture(lang, []string{"a", "b", "c"})
			os.Setenv("GT_FIELD_CANDIDATES", "1")
			defer os.Unsetenv("GT_FIELD_CANDIDATES")
			on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
			tg, first := fieldBasedTargets(on)
			if len(tg) != 3 {
				t.Fatalf("K==3: reachable ∩ exported must be {2,3,4}; got %v", tg)
			}
			if first.CandidateCount != 3 {
				t.Errorf("K==3: candidate_count must be 3; got %d", first.CandidateCount)
			}
			if first.Confidence != 0.4 {
				t.Errorf("K==3 (3<=K<=5): ambiguity gradient must decay to conf 0.4 (below the 0.5 "+
					"fact/traversal floor), not the old flat 0.5; got %v", first.Confidence)
			}
			// conf 0.4 < 0.5 → SPECULATIVE: the edge is STORED (LSP residual can resolve it)
			// but below the fact/closure floor, so it can no longer pose as reachable fact.
			if first.TrustTier != "SPECULATIVE" {
				t.Errorf("K==3 at conf 0.4 must be SPECULATIVE (below the 0.5 fact floor); got %q", first.TrustTier)
			}
		})
	}
}

// K==1: the intersection collapses to one reachable+exported candidate. The 1.9 provenance
// gate (caller imports the file) promotes ONE notch (0.5→0.6) — still CANDIDATE, still < 0.9.
func TestResolve_FieldCandidates_KOne_PromotesButStaysBelowVerified(t *testing.T) {
	for _, lang := range []string{"javascript", "python"} {
		t.Run(lang, func(t *testing.T) {
			clearNonMetaGlobals()
			// caller imports ONLY a. Reachable ∩ exported = {2}.
			calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := fieldCandFixture(lang, []string{"a"})
			os.Setenv("GT_FIELD_CANDIDATES", "1")
			defer os.Unsetenv("GT_FIELD_CANDIDATES")
			on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
			tg, first := fieldBasedTargets(on)
			if len(tg) != 1 || tg[0] != 2 {
				t.Fatalf("K==1: field_based set must be exactly {2}; got %v", tg)
			}
			if first.Confidence != 0.6 {
				t.Errorf("K==1 with provenance must promote to conf 0.6 (one notch); got %v", first.Confidence)
			}
			if first.TrustTier != "CANDIDATE" || first.Confidence >= 0.9 {
				t.Errorf("K==1 must stay CANDIDATE and strictly below verified (0.9); got tier=%q conf=%v", first.TrustTier, first.Confidence)
			}
			if first.EvidenceType != "field_based_unique" {
				t.Errorf("K==1 evidence must be field_based_unique; got %q", first.EvidenceType)
			}
		})
	}
}

// Empty intersection → drop (correct-or-quiet): the caller imports NONE of the defs, so no
// candidate is reachable. The call is dropped — NOT re-minted as an arbitrary name_match.
func TestResolve_FieldCandidates_EmptyIntersectionDrops(t *testing.T) {
	for _, lang := range []string{"javascript", "python"} {
		t.Run(lang, func(t *testing.T) {
			clearNonMetaGlobals()
			calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := fieldCandFixture(lang, nil) // no imports
			os.Setenv("GT_FIELD_CANDIDATES", "1")
			defer os.Unsetenv("GT_FIELD_CANDIDATES")
			on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
			if tg, _ := fieldBasedTargets(on); len(tg) != 0 {
				t.Errorf("empty intersection must DROP; got field_based %v", tg)
			}
			if got := nameMatchCount(on, 1); got != 0 {
				t.Errorf("empty intersection must DROP, not fall back to a name_match guess; got %d name_match", got)
			}
		})
	}
}

// Byte-identical-off + determinism (both modes) over the completeness fixture.
func TestResolve_FieldCandidates_ByteIdenticalOff_AndDeterminism(t *testing.T) {
	for _, lang := range []string{"javascript", "python"} {
		clearNonMetaGlobals()
		calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := fieldCandFixture(lang, []string{"a", "b", "d"})

		os.Unsetenv("GT_FIELD_CANDIDATES")
		a := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
		b := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
		if len(a) != len(b) {
			t.Fatalf("[%s] OFF determinism: edge count differs %d vs %d", lang, len(a), len(b))
		}
		for i := range a {
			if a[i] != b[i] {
				t.Fatalf("[%s] OFF determinism: edge %d differs %+v vs %+v", lang, i, a[i], b[i])
			}
		}

		os.Setenv("GT_FIELD_CANDIDATES", "1")
		c := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
		d := edgeSet(Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta))
		os.Unsetenv("GT_FIELD_CANDIDATES")
		if len(c) != len(d) {
			t.Fatalf("[%s] ON determinism: edge count differs %d vs %d", lang, len(c), len(d))
		}
		for i := range c {
			if c[i] != d[i] {
				t.Fatalf("[%s] ON determinism: edge %d differs %+v vs %+v", lang, i, c[i], d[i])
			}
		}
		// The OFF set must differ from ON (OFF = 1 name_match, ON = 2 field_based) — proves
		// the flag actually changes behavior while OFF stays the pre-change set.
		if len(a) == len(c) {
			t.Errorf("[%s] sanity: OFF and ON edge counts should differ (1 name_match vs 2 field_based)", lang)
		}
	}
}

// MUTATION companion. The import-reachability filter is what excludes the unreachable
// c(4). Neuter it (fieldCandidatesReachabilityGate=false) and the set must WRONGLY admit
// c(4) — proving the filter is load-bearing. Restores the gate.
func TestResolve_FieldCandidates_Mutation_ReachabilityFilterLoadBearing(t *testing.T) {
	os.Setenv("GT_FIELD_CANDIDATES", "1")
	defer os.Unsetenv("GT_FIELD_CANDIDATES")
	saved := fieldCandidatesReachabilityGate
	fieldCandidatesReachabilityGate = false // NEUTER the import-reachability filter
	defer func() { fieldCandidatesReachabilityGate = saved }()

	for _, lang := range []string{"javascript", "python"} {
		clearNonMetaGlobals()
		calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta := fieldCandFixture(lang, []string{"a", "b", "d"})
		on := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
		tg, _ := fieldBasedTargets(on)
		hasUnreachable := false
		for _, id := range tg {
			if id == 4 { // svc/c handle — NOT imported, unreachable in production
				hasUnreachable = true
			}
		}
		if !hasUnreachable {
			t.Errorf("[%s] MUTATION GUARD: with the import-reachability filter neutered, the UNREACHABLE "+
				"c(4) must leak into the set — its absence proves the filter was not actually load-bearing (set=%v)", lang, tg)
		}
	}
}
