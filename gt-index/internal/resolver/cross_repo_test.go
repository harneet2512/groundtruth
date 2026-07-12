package resolver

import "testing"

// SM-9a CROSS-REPO resolver — CONFIDENCE-TIERED, CORRECT-OR-QUIET.
//
// The product invariant under test: a cross-repo edge exists ONLY when it is
// coordinate-verified (import module path == another repo's package coordinate AND
// the bound symbol is EXPORTED in that repo's package). Two repos that merely share
// a symbol NAME, with no coordinate import between them, produce ZERO cross-repo
// edges — a bare cross-repo name match is never laundered as a fact.

// --- JS/named-import (import → exported symbol, IMPORTS edge) ------------------

// POSITIVE: repoA imports @org/mathlib name `add`; repoB (id 1) exports `add`.
// A CERTIFIED import_cross_repo edge with repo provenance. Disabling the coordinate
// match (matchModuleToCoord) reddens this — the "revert the union-resolver" mutation.
func TestCrossRepo_ImportToExportedSymbol_Certified(t *testing.T) {
	coords := []RepoCoord{
		{RepoID: 0, Root: "/repoA", JSPackages: []string{"@org/app"}},
		{RepoID: 1, Root: "/repoB", JSPackages: []string{"@org/mathlib"}},
	}
	nodes := []CrossRepoNode{
		{ID: 1, RepoID: 0, Name: "run", FilePath: "src/main.js", IsExported: true},
		{ID: 2, RepoID: 1, Name: "add", FilePath: "src/index.js", IsExported: true},
		{ID: 3, RepoID: 1, Name: "unused", FilePath: "src/index.js", IsExported: true},
	}
	imports := []CrossRepoImport{
		{RepoID: 0, File: "src/main.js", ModulePath: "@org/mathlib", ImportedName: "add", Line: 1, SourceAnchorID: 1},
	}
	edges := ResolveCrossRepoImports(imports, nodes, coords)
	if len(edges) != 1 {
		t.Fatalf("want 1 cross-repo edge, got %d: %+v", len(edges), edges)
	}
	e := edges[0]
	if e.SourceID != 1 || e.TargetID != 2 {
		t.Errorf("edge endpoints=%d->%d want 1->2 (run->add, NOT unused)", e.SourceID, e.TargetID)
	}
	if e.Method != "import_cross_repo" || e.TrustTier != "CERTIFIED" || e.Confidence != crossRepoCertifiedConf {
		t.Errorf("trust=%q/%q/%.2f want import_cross_repo/CERTIFIED/%.2f", e.Method, e.TrustTier, e.Confidence, crossRepoCertifiedConf)
	}
	if e.TargetRepoID != 1 {
		t.Errorf("TargetRepoID=%d want 1", e.TargetRepoID)
	}
	if want := "target_repo_id=1"; !containsSub(e.Metadata, want) {
		t.Errorf("metadata %q missing provenance %q", e.Metadata, want)
	}
	if !containsSub(e.Metadata, "resolved_by=js_package") {
		t.Errorf("metadata %q missing resolved_by=js_package", e.Metadata)
	}
}

// DROP: repoA and repoB both export `add`, but repoA imports it from `lodash`
// (external, matches NO repo coordinate). A same-named-but-unrelated symbol must NOT
// produce a cross-repo edge. Adding a name-match fallback would launder an edge here.
func TestCrossRepo_SameNamedButUnrelated_NoEdge(t *testing.T) {
	coords := []RepoCoord{
		{RepoID: 0, Root: "/repoA", JSPackages: []string{"@org/app"}},
		{RepoID: 1, Root: "/repoB", JSPackages: []string{"@org/mathlib"}},
	}
	nodes := []CrossRepoNode{
		{ID: 1, RepoID: 0, Name: "add", FilePath: "src/main.js", IsExported: true},
		{ID: 2, RepoID: 1, Name: "add", FilePath: "src/index.js", IsExported: true},
	}
	imports := []CrossRepoImport{
		{RepoID: 0, File: "src/main.js", ModulePath: "lodash", ImportedName: "add", Line: 1, SourceAnchorID: 1},
	}
	if edges := ResolveCrossRepoImports(imports, nodes, coords); len(edges) != 0 {
		t.Fatalf("DROP violated: a same-named symbol imported from a NON-coordinate module produced %d cross-repo edges: %+v", len(edges), edges)
	}
}

// NON-EXPORTED target: coordinate matches, but repoB's `add` is not exported → no edge.
func TestCrossRepo_NonExportedTarget_NoEdge(t *testing.T) {
	coords := []RepoCoord{
		{RepoID: 0, Root: "/repoA", JSPackages: []string{"@org/app"}},
		{RepoID: 1, Root: "/repoB", JSPackages: []string{"@org/mathlib"}},
	}
	nodes := []CrossRepoNode{
		{ID: 1, RepoID: 0, Name: "run", FilePath: "src/main.js", IsExported: true},
		{ID: 2, RepoID: 1, Name: "add", FilePath: "src/index.js", IsExported: false}, // not exported
	}
	imports := []CrossRepoImport{
		{RepoID: 0, File: "src/main.js", ModulePath: "@org/mathlib", ImportedName: "add", Line: 1, SourceAnchorID: 1},
	}
	if edges := ResolveCrossRepoImports(imports, nodes, coords); len(edges) != 0 {
		t.Fatalf("non-exported target must not be a cross-repo import target; got %d edges", len(edges))
	}
}

// AMBIGUOUS: repoB exports `add` twice in the SAME imported package → CANDIDATE tier
// (never CERTIFIED), candidate_count=2. A wide guess is honestly de-rated, not laundered.
func TestCrossRepo_Ambiguous_Candidate(t *testing.T) {
	coords := []RepoCoord{
		{RepoID: 0, Root: "/repoA", JSPackages: []string{"@org/app"}},
		{RepoID: 1, Root: "/repoB", JSPackages: []string{"@org/mathlib"}},
	}
	nodes := []CrossRepoNode{
		{ID: 1, RepoID: 0, Name: "run", FilePath: "src/main.js", IsExported: true},
		{ID: 2, RepoID: 1, Name: "add", FilePath: "src/a.js", IsExported: true},
		{ID: 3, RepoID: 1, Name: "add", FilePath: "src/b.js", IsExported: true},
	}
	imports := []CrossRepoImport{
		{RepoID: 0, File: "src/main.js", ModulePath: "@org/mathlib", ImportedName: "add", Line: 1, SourceAnchorID: 1},
	}
	edges := ResolveCrossRepoImports(imports, nodes, coords)
	if len(edges) != 1 {
		t.Fatalf("want 1 (de-duped) cross-repo edge, got %d", len(edges))
	}
	if edges[0].TrustTier != "CANDIDATE" || edges[0].CandidateCount != 2 {
		t.Errorf("ambiguous edge tier=%q count=%d want CANDIDATE/2", edges[0].TrustTier, edges[0].CandidateCount)
	}
}

// --- Go/qualified-call (import-guided pkg.Sym → exported symbol, CALLS edge) ---

// POSITIVE: repoA `main` calls mathlib.Add; import github.com/org/repob/mathlib
// resolves to repoB's Go module. A CERTIFIED call_cross_repo edge main->Add. The
// package refinement means `mathlib.Add` binds Add in the mathlib/ dir only.
func TestCrossRepo_GoCallSite_Certified(t *testing.T) {
	coords := []RepoCoord{
		{RepoID: 0, Root: "/repoA", GoModule: "github.com/org/repoa"},
		{RepoID: 1, Root: "/repoB", GoModule: "github.com/org/repob"},
	}
	nodes := []CrossRepoNode{
		{ID: 1, RepoID: 0, Name: "main", FilePath: "main.go", IsExported: false},
		{ID: 2, RepoID: 1, Name: "Add", FilePath: "mathlib/add.go", IsExported: true},
		{ID: 3, RepoID: 1, Name: "Unused", FilePath: "mathlib/add.go", IsExported: true},
	}
	imports := []CrossRepoImport{
		{RepoID: 0, File: "main.go", ModulePath: "github.com/org/repob/mathlib", ImportedName: "mathlib", Line: 3, SourceAnchorID: 1},
	}
	calls := []CrossRepoCall{
		{RepoID: 0, CallerID: 1, CalleeName: "Add", CalleeQualified: "mathlib.Add", File: "main.go", Line: 6},
	}
	edges := ResolveCrossRepoCalls(calls, imports, nodes, coords)
	if len(edges) != 1 {
		t.Fatalf("want 1 cross-repo CALLS edge, got %d: %+v", len(edges), edges)
	}
	e := edges[0]
	if e.SourceID != 1 || e.TargetID != 2 {
		t.Errorf("edge=%d->%d want 1->2 (main->Add, NOT Unused)", e.SourceID, e.TargetID)
	}
	if e.Method != "call_cross_repo" || e.TrustTier != "CERTIFIED" {
		t.Errorf("trust=%q/%q want call_cross_repo/CERTIFIED", e.Method, e.TrustTier)
	}
	if !containsSub(e.Metadata, "resolved_by=go_module") || !containsSub(e.Metadata, "target_repo_id=1") {
		t.Errorf("metadata %q missing go_module provenance", e.Metadata)
	}
}

// DROP (Go): a qualified call whose qualifier is NOT a coordinate-bound import alias
// must not resolve cross-repo, even when a same-named exported symbol exists elsewhere.
func TestCrossRepo_GoCallUnboundQualifier_NoEdge(t *testing.T) {
	coords := []RepoCoord{
		{RepoID: 0, Root: "/repoA", GoModule: "github.com/org/repoa"},
		{RepoID: 1, Root: "/repoB", GoModule: "github.com/org/repob"},
	}
	nodes := []CrossRepoNode{
		{ID: 1, RepoID: 0, Name: "main", FilePath: "main.go"},
		{ID: 2, RepoID: 1, Name: "Add", FilePath: "mathlib/add.go", IsExported: true},
	}
	// repoA imports a DIFFERENT (external) module — no coordinate binding for `other`.
	imports := []CrossRepoImport{
		{RepoID: 0, File: "main.go", ModulePath: "example.com/other", ImportedName: "other", Line: 3, SourceAnchorID: 1},
	}
	calls := []CrossRepoCall{
		{RepoID: 0, CallerID: 1, CalleeName: "Add", CalleeQualified: "other.Add", File: "main.go", Line: 6},
	}
	if edges := ResolveCrossRepoCalls(calls, imports, nodes, coords); len(edges) != 0 {
		t.Fatalf("unbound qualifier must not resolve cross-repo; got %d edges", len(edges))
	}
}

// Package-subpath refinement (Go): `mathlib.Add` must bind Add in the mathlib/ dir,
// NOT a same-named exported Add in a DIFFERENT package of the same target repo.
func TestCrossRepo_GoPackageRefinement(t *testing.T) {
	coords := []RepoCoord{
		{RepoID: 0, Root: "/repoA", GoModule: "github.com/org/repoa"},
		{RepoID: 1, Root: "/repoB", GoModule: "github.com/org/repob"},
	}
	nodes := []CrossRepoNode{
		{ID: 1, RepoID: 0, Name: "main", FilePath: "main.go"},
		{ID: 2, RepoID: 1, Name: "Add", FilePath: "mathlib/add.go", IsExported: true},
		{ID: 3, RepoID: 1, Name: "Add", FilePath: "otherpkg/x.go", IsExported: true}, // wrong package
	}
	imports := []CrossRepoImport{
		{RepoID: 0, File: "main.go", ModulePath: "github.com/org/repob/mathlib", ImportedName: "mathlib", Line: 3, SourceAnchorID: 1},
	}
	calls := []CrossRepoCall{
		{RepoID: 0, CallerID: 1, CalleeName: "Add", CalleeQualified: "mathlib.Add", File: "main.go", Line: 6},
	}
	edges := ResolveCrossRepoCalls(calls, imports, nodes, coords)
	if len(edges) != 1 || edges[0].TargetID != 2 {
		t.Fatalf("package refinement failed: want single edge to id=2 (mathlib/add.go), got %+v", edges)
	}
	if edges[0].TrustTier != "CERTIFIED" {
		t.Errorf("unique in-package target should be CERTIFIED, got %q", edges[0].TrustTier)
	}
}

func containsSub(s, sub string) bool {
	return len(sub) == 0 || (len(s) >= len(sub) && indexOf(s, sub) >= 0)
}
func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
