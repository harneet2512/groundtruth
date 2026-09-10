package main

// RED witnesses for the batch-amend id-space defect in main.go: a batch amend
// retains the unchanged files' node ids and re-inserts the edited file's nodes
// at the TOP of the AUTOINCREMENT id space, so a cross-file pick that reads raw
// id order — `id < best` or first-in-slice — names a different logical node
// under an amend than under a full rebuild. The tests below build the SAME
// logical graph under both id layouts and assert the pick is identical
// (content key: file_path, start_line, id).

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/resolver"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// TestBuildInheritanceMapParentPickIsLayoutInvariant: `class Child(T)` in
// caller.py must resolve the base T to the same declaration no matter where
// the edited file's nodes land in the id space. The old resolveClass fallback
// returned the first class-labelled id in the nameIndex slice — insertion
// order, which a batch amend / -file reindex turns into id-space order — so
// the resolved parent (and every inherited CALLS lookup built on it) flipped
// between the two layouts.
func TestBuildInheritanceMapParentPickIsLayoutInvariant(t *testing.T) {
	root := t.TempDir()
	write := func(rel, src string) {
		p := filepath.Join(root, rel)
		if err := os.WriteFile(p, []byte(src), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("caller.py", "class Child(T):\n    pass\n")
	write("a_other.py", "class T:\n    pass\n")
	write("z_edited.py", "class T:\n    pass\n")
	files := []walker.SourceFile{
		{Path: "caller.py", AbsPath: filepath.Join(root, "caller.py"), Language: "python"},
		{Path: "a_other.py", AbsPath: filepath.Join(root, "a_other.py"), Language: "python"},
		{Path: "z_edited.py", AbsPath: filepath.Join(root, "z_edited.py"), Language: "python"},
	}

	// Rebuild layout: the edited file's T carries the low id and sits FIRST in
	// the nameIndex slice (id order). Amend layout: same class renumbered to 90
	// and listed last. Both must bind Child -> the SAME logical T.
	rebuild := map[int64]resolver.NodeMeta{
		6: {Label: "Class", Name: "Child", File: "caller.py", StartLine: 1},
		2: {Label: "Class", Name: "T", File: "z_edited.py", StartLine: 1},
		4: {Label: "Class", Name: "T", File: "a_other.py", StartLine: 1},
	}
	rebuildIDs := map[string][]int64{"Child": {6}, "T": {2, 4}}
	amend := map[int64]resolver.NodeMeta{
		6:  {Label: "Class", Name: "Child", File: "caller.py", StartLine: 1},
		4:  {Label: "Class", Name: "T", File: "a_other.py", StartLine: 1},
		90: {Label: "Class", Name: "T", File: "z_edited.py", StartLine: 1},
	}
	amendIDs := map[string][]int64{"Child": {6}, "T": {4, 90}}

	gotRebuild := buildInheritanceMap(files, root, rebuildIDs, rebuild)[6]
	gotAmend := buildInheritanceMap(files, root, amendIDs, amend)[6]
	if len(gotRebuild) != 1 || len(gotAmend) != 1 {
		t.Fatalf("Child should inherit exactly one base: rebuild %v, amend %v", gotRebuild, gotAmend)
	}
	pr, pa := rebuild[gotRebuild[0]], amend[gotAmend[0]]
	if pr.File != pa.File || pr.StartLine != pa.StartLine {
		t.Fatalf("base T resolved to different declarations: rebuild -> %s:%d, amend -> %s:%d",
			pr.File, pr.StartLine, pa.File, pa.StartLine)
	}
	if pr.File != "a_other.py" {
		t.Fatalf("base T resolved to %s, want a_other.py (content-smallest class)", pr.File)
	}
}

// TestAssertionTieBreakIsLayoutInvariant: two same-named production functions
// at equal score used to break the tie on lowest node id — which flips when an
// amend renumbers the edited file's nodes. The tie must break on (file_path,
// start_line, id) instead.
func TestAssertionTieBreakIsLayoutInvariant(t *testing.T) {
	// test file test_x.py asserts on helper(); helper exists in a_other.py and
	// in the edited z_edited.py. Both candidates score 3.5 (LCBA 3.0 + non-test
	// 0.5) — a pure tie decided by the pick rule.
	build := func(editedID, otherID int64) (parser.AssertionRef, []*store.Node, []int64, map[string][]int64, map[int64]string) {
		testFile := "pkg/test_x.py"
		nodes := []*store.Node{
			{Label: "Function", Name: "test_x", FilePath: testFile, StartLine: 3, IsTest: true},
			{Label: "Function", Name: "helper", FilePath: "pkg/z_edited.py", StartLine: 10},
			{Label: "Function", Name: "helper", FilePath: "pkg/a_other.py", StartLine: 10},
		}
		ids := []int64{1, editedID, otherID}
		nameToNodeIDs := map[string][]int64{"helper": {editedID, otherID}}
		fileOf := map[int64]string{1: testFile, editedID: "pkg/z_edited.py", otherID: "pkg/a_other.py"}
		a := parser.AssertionRef{TestNodeIdx: 0, Kind: "assert", Expression: "assert helper(x)", Line: 4}
		return a, nodes, ids, nameToNodeIDs, fileOf
	}

	a, nodes, ids, names, fileOf := build(2, 4)
	gotRebuild, scoreA := resolveAssertionTarget(a, nodes, ids, names, nil, nil, fileOf)
	a2, nodes2, ids2, names2, fileOf2 := build(90, 4)
	gotAmend, scoreB := resolveAssertionTarget(a2, nodes2, ids2, names2, nil, nil, fileOf2)

	if gotRebuild == 0 || gotAmend == 0 {
		t.Fatalf("assertion target unresolved: rebuild %d (score %.2f), amend %d (score %.2f)",
			gotRebuild, scoreA, gotAmend, scoreB)
	}
	if fileOf[gotRebuild] != fileOf2[gotAmend] {
		t.Fatalf("assertion tie picked different logical targets: rebuild -> %s, amend -> %s",
			fileOf[gotRebuild], fileOf2[gotAmend])
	}
	if fileOf[gotRebuild] != "pkg/a_other.py" {
		t.Fatalf("tie broke to %s, want pkg/a_other.py (content-smallest file)", fileOf[gotRebuild])
	}
}
