package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// A `from pkg import mod` statement binds a submodule: the import index admits
// it through its module+name fallback ("pkg" names no indexed file, but
// "pkg.mod" resolves to pkg/mod.py), so a `mod.helper()` call mints an
// "import" candidate the publication contract then holds to import_binding's
// chain requirement. The per-candidate evidence must reproduce the same
// resolution the index used to admit the candidate -- an empty chain here is
// not "no import happened" (one did), it is the evidence computation
// disagreeing with the index about which files the import can bind, and the
// atomic attach rejects the whole analysis for it.
func TestResolveWithProvenanceBindsSubmoduleFallbackImportEvidence(t *testing.T) {
	calls := []parser.CallRef{{
		CalleeName: "helper", CalleeQualified: "mod.helper",
		File: "main.py", Line: 3, DispatchForm: "virtual",
	}}
	imports := []parser.ImportRef{
		{ImportedName: "mod", ModulePath: "pkg", File: "main.py"},
	}
	_, traces := ResolveWithProvenance(calls,
		map[string][]int64{"helper": {2}},
		map[string]map[string][]int64{"pkg/mod.py": {"helper": {2}}},
		[]int64{1}, imports,
		// "pkg" is deliberately absent: the bare module path resolves nothing
		// and only the index's combined "pkg.mod" fallback reaches the file.
		map[string][]string{"pkg.mod": {"pkg/mod.py"}},
		map[int64]NodeMeta{1: {File: "main.py"}, 2: {File: "pkg/mod.py"}})
	if len(traces) != 1 || len(traces[0].CandidateNodeIDs) != 1 || traces[0].CandidateNodeIDs[0] != 2 {
		t.Fatalf("submodule import did not mint the expected candidate: %+v", traces)
	}
	if traces[0].Mechanism != "import" {
		t.Fatalf("candidate was not import-derived: mechanism=%q", traces[0].Mechanism)
	}
	if got := traces[0].CandidateImportChains[2]; len(got) != 1 || got[0] != "pkg:mod" {
		t.Fatalf("import-derived candidate carries no chain: CandidateImportChains[2]=%v", got)
	}
}

// The same fallback through the slash form: `from a.b import c` where neither
// "a.b" nor "a.b.c" resolve but "a/b/c" does.
func TestResolveWithProvenanceBindsSlashFallbackImportEvidence(t *testing.T) {
	calls := []parser.CallRef{{
		CalleeName: "c", CalleeQualified: "c.run",
		File: "main.py", Line: 3, DispatchForm: "virtual",
	}}
	imports := []parser.ImportRef{
		{ImportedName: "c", ModulePath: "a.b", File: "main.py"},
	}
	_, traces := ResolveWithProvenance(calls,
		map[string][]int64{"run": {2}},
		map[string]map[string][]int64{"a/b/c.py": {"run": {2}}},
		[]int64{1}, imports,
		// resolveModulePath maps dotted "a.b.c" to slash "a/b/c" internally, so
		// the dotted fallback hits first. To exercise the slash fallback itself
		// the module path must already be slash-shaped.
		map[string][]string{"a/b/c": {"a/b/c.py"}},
		map[int64]NodeMeta{1: {File: "main.py"}, 2: {File: "a/b/c.py"}})
	if len(traces) != 1 || len(traces[0].CandidateNodeIDs) != 1 || traces[0].CandidateNodeIDs[0] != 2 {
		t.Fatalf("slash-fallback import did not mint the expected candidate: %+v", traces)
	}
	if got := traces[0].CandidateImportChains[2]; len(got) != 1 || got[0] != "a.b:c" {
		t.Fatalf("import-derived candidate carries no chain: CandidateImportChains[2]=%v", got)
	}
}
