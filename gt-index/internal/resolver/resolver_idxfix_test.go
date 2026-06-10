package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// ----------------------------------------------------------------------------
// Indexer-fix batch (#B5 strategy reorder, #B8b versioned-module double-append).
// RED before the matching fix, GREEN after.
// ----------------------------------------------------------------------------

// #B5: a qualified call on a DECLARED-TYPE receiver (`command.run()` where the
// caller declares `command: Command`) must resolve via the type-aware rung
// 1.94a (type_flow, conf 0.9) — NOT be demoted to a 0.2 name_match by the old
// Strategy-1.9 single-candidate short-circuit that ran before 1.93-1.98.
func TestResolve_Reorder_TypedReceiverResolvesTypeFlowNotDemoted(t *testing.T) {
	files := []string{"a.py", "b.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	// `run` has exactly ONE global definition (Command.run, id 4) — the old code
	// hit the 1.9 single-candidate path first and demoted the qualified call to
	// name_match conf 0.2 before 1.94a could use the declared param type.
	nodeIDs := map[string][]int64{
		"caller":  {1},
		"Command": {3},
		"run":     {4},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"a.py": {"caller": {1}},
		"b.py": {"Command": {3}, "run": {4}},
	}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", File: "a.py", Name: "caller"},
		3: {Label: "Class", File: "b.py", Name: "Command"},
		4: {Label: "Method", File: "b.py", Name: "run", ParentID: 3},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "run", CalleeQualified: "command.run", Line: 5, File: "a.py"},
	}
	callerIDs := []int64{1}

	// The caller declares `command: Command` (the parser's `param` property).
	SetParamTypeIndex(map[int64]map[string]string{1: {"command": "Command"}})
	defer SetParamTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	if len(resolved) != 1 {
		t.Fatalf("expected 1 resolved edge, got %d: %+v", len(resolved), resolved)
	}
	r := resolved[0]
	if r.Method != "type_flow" {
		t.Errorf("method = %q (conf %.2f), want type_flow — the 1.9 demote starved the type-aware rung", r.Method, r.Confidence)
	}
	if r.TargetNodeID != 4 {
		t.Errorf("target = %d, want 4 (Command.run)", r.TargetNodeID)
	}
	if r.Confidence != 0.9 {
		t.Errorf("confidence = %.2f, want 0.9 (declared-type receiver)", r.Confidence)
	}
}

// #B5: a builtin-NAMED method on a RECEIVER-PROVEN internal class must resolve.
// `Config.get()` where Config is a project class defining get() used to be
// dropped by the builtin guard before 1.95 ever ran; the receiver IS internal
// (the qualifier names the class), so the call is an internal edge.
func TestResolve_Reorder_BuiltinNameOnProvenReceiverResolves(t *testing.T) {
	files := []string{"a.py", "b.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	nodeIDs := map[string][]int64{
		"caller": {1},
		"Config": {3},
		"get":    {4},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"a.py": {"caller": {1}},
		"b.py": {"Config": {3}, "get": {4}},
	}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", File: "a.py", Name: "caller"},
		3: {Label: "Class", File: "b.py", Name: "Config"},
		4: {Label: "Method", File: "b.py", Name: "get", ParentID: 3},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "Config.get", Line: 5, File: "a.py"},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	var found bool
	for _, r := range resolved {
		if r.TargetNodeID == 4 {
			found = true
			if r.Method != "type_flow" {
				t.Errorf("method = %q, want type_flow (1.95: qualifier is a known class)", r.Method)
			}
		}
	}
	if !found {
		t.Fatalf("Config.get() (receiver-proven internal class) was dropped by the builtin guard; got %+v", resolved)
	}
}

// #B5 precision guard: a builtin-named method on an UNPROVEN receiver must
// still be dropped — even when an internal class defines a same-named method
// that the receiver-unproven rungs (1.94 impl_method / 1.98 unique_method)
// would otherwise claim. `cfg.get()` with no type information is a dict/config
// builtin call, not an edge to SomeClass.get.
func TestResolve_BuiltinNameOnUnprovenReceiverStillDropped(t *testing.T) {
	files := []string{"a.py", "b.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	// `get` is a method on exactly ONE internal class — without the guard,
	// 1.94/1.98 would resolve every untyped x.get() in the repo to it.
	nodeIDs := map[string][]int64{
		"caller":   {1},
		"Settings": {3},
		"get":      {4},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"a.py": {"caller": {1}},
		"b.py": {"Settings": {3}, "get": {4}},
	}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", File: "a.py", Name: "caller"},
		3: {Label: "Class", File: "b.py", Name: "Settings"},
		4: {Label: "Method", File: "b.py", Name: "get", ParentID: 3},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "cfg.get", Line: 5, File: "a.py"},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	for _, r := range resolved {
		if r.TargetNodeID == 4 {
			t.Errorf("untyped cfg.get() claimed Settings.get via %q (conf %.2f) — builtin guard must hold on receiver-unproven rungs", r.Method, r.Confidence)
		}
	}
}

// #B8b: RegisterGoModulePaths on a VERSIONED module must register each
// module-prefixed key exactly ONCE (no double-append of the same file) and
// must not mint recursive garbage keys like "mod/mod/v2/pkg".
func TestRegisterGoModulePaths_VersionedNoDoubleAppend(t *testing.T) {
	fm := BuildFileMap(
		[]string{"pkg/auth/login.go"},
		[]string{"go"},
	)
	RegisterGoModulePaths(fm, "example.com/proj/v2")

	for _, key := range []string{"example.com/proj/v2/pkg/auth", "example.com/proj/pkg/auth"} {
		files, ok := fm[key]
		if !ok {
			t.Errorf("expected key %q in file map", key)
			continue
		}
		seen := map[string]int{}
		for _, f := range files {
			seen[f]++
		}
		for f, n := range seen {
			if n > 1 {
				t.Errorf("key %q: file %q appended %d times (want 1) — the double-append", key, f, n)
			}
		}
	}
	// The garbage recursive key the old versioned loop minted from the
	// already-mutated map must not exist.
	if _, ok := fm["example.com/proj/example.com/proj/v2/pkg/auth"]; ok {
		t.Error("recursive garbage key example.com/proj/example.com/proj/v2/pkg/auth was registered")
	}
}

// #B3: File-anchor nodes (label "File") must never be call-resolution targets.
// A barrel file's synthetic node named like a real symbol must not absorb a
// verified_unique edge.
func TestBuildNameIndex_ExcludesFileAnchorNodes(t *testing.T) {
	nodes := []store.Node{
		{Label: "File", Name: "utils", FilePath: "src/utils/index.ts"},
		{Label: "Function", Name: "caller", FilePath: "src/app.ts"},
	}
	nameIndex, fileIndex := BuildNameIndex(nil, nodes, []int64{10, 11})
	if ids := nameIndex["utils"]; len(ids) != 0 {
		t.Errorf("File-anchor node registered in name index: %v", ids)
	}
	if ids := nameIndex["caller"]; len(ids) != 1 || ids[0] != 11 {
		t.Errorf("regular node missing from name index: %v", ids)
	}
	if m := fileIndex["src/utils/index.ts"]; len(m) != 0 {
		t.Errorf("File-anchor node registered in file index: %v", m)
	}
}
