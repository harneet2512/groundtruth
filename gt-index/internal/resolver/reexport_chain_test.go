package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// Re-export chains: `pkg/__init__.py` holds `from .impl import Thing`, so a
// caller's `from pkg import Thing` binds through the package surface to the
// defining file. Before the chase, the import index bound `Thing` to
// `pkg/__init__.py` — a file that defines no `Thing` node — so every
// `Thing()` callsite fell back to name_match at ~0.2 instead of the
// import-verified edge the graph should mint.
//
// Python's dominant import shape (aiomonitor, click, requests all re-export
// their public API through __init__.py) made this the largest single recall
// gap on the F1 audit: 3 of 9,726 callsites resolved `import`.
func TestReExportChainResolvesToDefiningFile(t *testing.T) {
	files := []string{"pkg/__init__.py", "pkg/impl.py", "caller.py"}
	langs := []string{"python", "python", "python"}
	fileMap := BuildFileMap(files, langs)

	imports := []parser.ImportRef{
		// pkg/__init__.py: `from .impl import Thing` — re-export surface
		{File: "pkg/__init__.py", ModulePath: ".impl", ImportedName: "Thing", Line: 1},
		// caller.py: `from pkg import Thing`
		{File: "caller.py", ModulePath: "pkg", ImportedName: "Thing", Line: 1},
	}
	calls := []parser.CallRef{
		{CalleeName: "Thing", File: "caller.py", Line: 3},
	}
	nodeIDs := map[string][]int64{"Thing": {10}}
	fileNodeIDs := map[string]map[string][]int64{
		"pkg/impl.py": {"Thing": {10}},
	}
	callerNodeIDs := []int64{1}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "use", File: "caller.py"},
		10: {Label: "Class", Name: "Thing", File: "pkg/impl.py"},
	}

	resolved, _ := resolveInternal(calls, nodeIDs, fileNodeIDs, callerNodeIDs, imports, fileMap, true, meta)
	if len(resolved) != 1 {
		t.Fatalf("resolved %d calls, want exactly 1 (import-verified Thing)", len(resolved))
	}
	if resolved[0].TargetNodeID != 10 {
		t.Fatalf("Thing resolved to node %d, want 10 (pkg/impl.py defining file)", resolved[0].TargetNodeID)
	}
	if resolved[0].Method != "import" {
		t.Fatalf("method %q, want import — re-export chase must bind at import tier, not name_match", resolved[0].Method)
	}
	if resolved[0].Confidence != 1.0 {
		t.Fatalf("confidence %v, want 1.0 — single unambiguous chased target", resolved[0].Confidence)
	}
}

// Multi-hop: `a/__init__.py` re-exports from `b/__init__.py`, which re-exports
// from `b/impl.py`. The chase is transitive, depth-capped, and cycle-safe.
func TestReExportChainTransitive(t *testing.T) {
	files := []string{"a/__init__.py", "b/__init__.py", "b/impl.py", "caller.py"}
	langs := []string{"python", "python", "python", "python"}
	fileMap := BuildFileMap(files, langs)

	imports := []parser.ImportRef{
		{File: "a/__init__.py", ModulePath: "b", ImportedName: "Widget", Line: 1},
		{File: "b/__init__.py", ModulePath: ".impl", ImportedName: "Widget", Line: 1},
		{File: "caller.py", ModulePath: "a", ImportedName: "Widget", Line: 1},
	}
	calls := []parser.CallRef{
		{CalleeName: "Widget", File: "caller.py", Line: 2},
	}
	nodeIDs := map[string][]int64{"Widget": {20}}
	fileNodeIDs := map[string]map[string][]int64{
		"b/impl.py": {"Widget": {20}},
	}
	callerNodeIDs := []int64{1}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "use", File: "caller.py"},
		20: {Label: "Class", Name: "Widget", File: "b/impl.py"},
	}

	resolved, _ := resolveInternal(calls, nodeIDs, fileNodeIDs, callerNodeIDs, imports, fileMap, true, meta)
	if len(resolved) != 1 {
		t.Fatalf("resolved %d calls, want exactly 1", len(resolved))
	}
	if resolved[0].TargetNodeID != 20 || resolved[0].Method != "import" {
		t.Fatalf("got target=%d method=%q, want 20/import — two-hop re-export must reach b/impl.py",
			resolved[0].TargetNodeID, resolved[0].Method)
	}
}

// `..pkg` (two dots) climbs one package level: `pkg/sub/mod.py` doing
// `from ..helpers import log` resolves to `pkg/helpers.py`.
func TestPythonRelativeModuleDoubleDot(t *testing.T) {
	files := []string{"pkg/helpers.py", "pkg/sub/mod.py"}
	langs := []string{"python", "python"}
	fileMap := BuildFileMap(files, langs)

	imports := []parser.ImportRef{
		{File: "pkg/sub/mod.py", ModulePath: "..helpers", ImportedName: "log", Line: 1},
	}
	calls := []parser.CallRef{
		{CalleeName: "log", File: "pkg/sub/mod.py", Line: 4},
	}
	nodeIDs := map[string][]int64{"log": {30}}
	fileNodeIDs := map[string]map[string][]int64{
		"pkg/helpers.py": {"log": {30}},
	}
	callerNodeIDs := []int64{1}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "run", File: "pkg/sub/mod.py"},
		30: {Label: "Function", Name: "log", File: "pkg/helpers.py"},
	}

	resolved, _ := resolveInternal(calls, nodeIDs, fileNodeIDs, callerNodeIDs, imports, fileMap, true, meta)
	if len(resolved) != 1 || resolved[0].TargetNodeID != 30 || resolved[0].Method != "import" {
		t.Fatalf("got %+v, want target=30 method=import — `..helpers` must climb to pkg/helpers.py", resolved)
	}
}

// Cycle safety: `a/__init__` imports X from `b`, `b/__init__` imports X from
// `a`. The chase must terminate and still deliver both surface files — the
// binding pass then picks a real def if either package defines X.
func TestReExportCycleTerminates(t *testing.T) {
	files := []string{"a/__init__.py", "b/__init__.py", "caller.py"}
	langs := []string{"python", "python", "python"}
	fileMap := BuildFileMap(files, langs)

	imports := []parser.ImportRef{
		{File: "a/__init__.py", ModulePath: "b", ImportedName: "X", Line: 1},
		{File: "b/__init__.py", ModulePath: "a", ImportedName: "X", Line: 1},
		{File: "caller.py", ModulePath: "a", ImportedName: "X", Line: 1},
	}
	calls := []parser.CallRef{
		{CalleeName: "X", File: "caller.py", Line: 2},
	}
	nodeIDs := map[string][]int64{"X": {40}}
	fileNodeIDs := map[string]map[string][]int64{
		"b/__init__.py": {"X": {40}},
	}
	callerNodeIDs := []int64{1}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "use", File: "caller.py"},
		40: {Label: "Class", Name: "X", File: "b/__init__.py"},
	}

	resolved, _ := resolveInternal(calls, nodeIDs, fileNodeIDs, callerNodeIDs, imports, fileMap, true, meta)
	if len(resolved) != 1 || resolved[0].TargetNodeID != 40 {
		t.Fatalf("got %+v, want target=40 — cyclic re-exports must terminate and bind the defining surface", resolved)
	}
}
