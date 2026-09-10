package taxonomy

// RED witness for the batch-amend id-space defect in buildIndex: the candidate
// index sorted by raw id, and emit() truncates at MaxTaxonomyCandidates — so
// once a name had >8 declarations, the RETAINED edge set depended on where the
// edited file's nodes sat in the AUTOINCREMENT id space. A batch amend
// re-inserts them at the top, so the same logical "Shape" set retained a
// different subset under an amend than under a rebuild — edges minted against
// different logical targets. Content order (file_path, start_line, id) retains
// the same subset either way.

import (
	"sort"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

func TestTruncatedCandidateSetIsLayoutInvariant(t *testing.T) {
	// One class implementing a name carried by MaxTaxonomyCandidates+1
	// declarations — a01.ts..a08.ts plus z_edited.ts (the AMENDED file).
	// Rebuild layout assigns z_edited's Shape the low id 2; the amend layout
	// renumbers it to 90, at the top of the id space.
	build := func(editedID int64) ([]*store.Node, []int64) {
		nodes := []*store.Node{node("Class", "Circle", "src.ts")}
		ids := []int64{1}
		for i, f := range []string{"a01.ts", "a02.ts", "a03.ts", "a04.ts", "a05.ts", "a06.ts", "a07.ts", "a08.ts"} {
			nodes = append(nodes, node("Interface", "Shape", f))
			ids = append(ids, int64(i+3)) // ids 3..10
		}
		nodes = append(nodes, node("Interface", "Shape", "z_edited.ts"))
		ids = append(ids, editedID)
		return nodes, ids
	}
	props := []parser.PropertyRef{
		{NodeIdx: 0, Kind: parser.PropImplementsType, Value: "Shape|" + specs.MechImplementsClause, Line: 1},
	}

	targetFiles := func(rows []*store.Edge, nodes []*store.Node, ids []int64) []string {
		fileOf := make(map[int64]string, len(ids))
		for i, id := range ids {
			if i < len(nodes) {
				fileOf[id] = nodes[i].FilePath
			}
		}
		var files []string
		for _, e := range edgesByKind(rows, specs.EdgeDeclaredImplements) {
			files = append(files, fileOf[e.TargetID])
		}
		sort.Strings(files)
		return files
	}

	nodesA, idsA := build(2)  // rebuild: edited file's node at a LOW id
	nodesB, idsB := build(90) // amend:   same node at a HIGH id
	filesA := targetFiles(DeriveEdges(nodesA, idsA, props), nodesA, idsA)
	filesB := targetFiles(DeriveEdges(nodesB, idsB, props), nodesB, idsB)

	if len(filesA) != specs.MaxTaxonomyCandidates || len(filesB) != specs.MaxTaxonomyCandidates {
		t.Fatalf("expected the retained set capped at %d in both layouts, got %d / %d",
			specs.MaxTaxonomyCandidates, len(filesA), len(filesB))
	}
	for i := range filesA {
		if filesA[i] != filesB[i] {
			t.Fatalf("truncated candidate set differs across id layouts:\nrebuild %v\namend   %v",
				filesA, filesB)
		}
	}
	// Content order keeps the eight a*.ts declarations and drops z_edited's —
	// the same subset in BOTH layouts.
	for _, f := range filesA {
		if f == "z_edited.ts" {
			t.Fatalf("z_edited.ts retained over an a*.ts declaration: %v", filesA)
		}
	}
}
