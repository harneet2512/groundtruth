package main

// incremental_structure.go — structural layers the per-file (-file) amend must
// re-derive so they match what a full build publishes. Each gap was found by
// the incremental-vs-clean convergence harness (convergence_test.go): the
// reparsed file lost its CONTAINS edges and every taxonomy edge
// (DECLARED_IMPLEMENTS, OVERRIDES, DECORATES, PARAM_TYPE, ACCESSES, ...)
// touching it, because -file never ran either derivation.

import (
	"database/sql"
	"fmt"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/taxonomy"
)

// Project-meta keys that state how far a graph is from a clean rebuild of the
// tree it names. A full build and a batch amend (-amend-parent) run the whole
// pipeline and are clean-equivalent (pinned by
// TestBatchAmendConvergesToCleanRebuild). A per-file amend re-derives only
// the named files: the gaps it leaves are listed, never implied.
const (
	metaGraphConvergence     = "graph_convergence_state"
	metaGraphConvergenceGaps = "graph_convergence_gaps"

	convergenceCleanEquivalent = "clean_equivalent"
	convergencePerFilePartial  = "per_file_partial"
)

// perFileConvergenceGaps names what a -file amend does NOT re-derive:
//   - unedited_file_edge_rebinding: CALLS/IMPORTS sourced in a file that is
//     not re-resolved after its target changed are never re-derived: edges
//     into the reparsed file are rebound by name (IncomingEdgeRef) or
//     dropped, and a call or import that names a symbol this edit ADDED or
//     RENAMED gets no edge. Within one dirty set this includes a dirty file
//     amended BEFORE the file defining its new target (paths run in order);
//   - resolution_overlay_absent: resolution_symbols/callsites/candidates and
//     the callsite fact nodes are not rebuilt (analysis_state=not_run);
//   - derived_layers_not_run: closure, cochange, communities, processes are
//     deleted and marked not_run;
//   - assertion_targets_rebound_by_identity: other files' assertions into the
//     file are rebound by (label, qualified_name) or cleared, not re-resolved.
var perFileConvergenceGaps = []string{
	"unedited_file_edge_rebinding",
	"resolution_overlay_absent",
	"derived_layers_not_run",
	"assertion_targets_rebound_by_identity",
}

// fileContainsEdges mirrors the full build's parent_id -> CONTAINS derivation
// for one reparsed file. parentLocal[i] is node i's 1-based parent index into
// the same file's node list (0 = no parent), as ParseResult records it.
func fileContainsEdges(nodes []*store.Node, parentLocal []int64, ids []int64) []*store.Edge {
	var out []*store.Edge
	for i, plocal := range parentLocal {
		pidx := int(plocal) - 1
		if plocal <= 0 || pidx < 0 || pidx >= len(ids) || i >= len(ids) || ids[pidx] <= 0 || ids[i] <= 0 {
			continue
		}
		out = append(out, &store.Edge{
			SourceID:           ids[pidx],
			TargetID:           ids[i],
			Type:               "CONTAINS",
			SourceFile:         nodes[i].FilePath,
			ResolutionMethod:   "structural",
			Confidence:         1.0,
			TrustTier:          "CERTIFIED",
			EvidenceType:       "parent_id",
			VerificationStatus: "verified",
		})
	}
	return out
}

// rederiveTaxonomyTx replaces every taxonomy edge with a fresh derivation over
// the whole post-amend graph. Taxonomy targets resolve by NAME across files,
// so a rename in the reparsed file changes edges sourced in other files too;
// only a whole-graph pass converges. It reads the parser-owned node and
// property rows (resolution fact nodes carry node_type and are excluded, as
// they are absent when the full build derives taxonomy).
func rederiveTaxonomyTx(tx *sql.Tx) (int, error) {
	placeholders := strings.TrimSuffix(strings.Repeat("?,", len(store.TaxonomyEdgeTypes)), ",")
	args := make([]interface{}, 0, len(store.TaxonomyEdgeTypes))
	for _, t := range store.TaxonomyEdgeTypes {
		args = append(args, t)
	}
	if _, err := tx.Exec(`DELETE FROM edges WHERE COALESCE(evidence_type,'') = 'syntax' AND type IN (`+placeholders+`)`, args...); err != nil {
		return 0, fmt.Errorf("clear taxonomy edges: %w", err)
	}
	nodes, ids, index, err := loadParserNodesTx(tx)
	if err != nil {
		return 0, err
	}
	props, err := loadPropertyRefsTx(tx, index)
	if err != nil {
		return 0, err
	}
	edges := taxonomy.DeriveEdges(nodes, ids, props)
	if err := store.BatchInsertEdgesTx(tx, edges); err != nil {
		return 0, fmt.Errorf("insert taxonomy edges: %w", err)
	}
	return len(edges), nil
}

func loadParserNodesTx(tx *sql.Tx) ([]*store.Node, []int64, map[int64]int, error) {
	rows, err := tx.Query(`SELECT id, label, name, COALESCE(qualified_name,''), file_path,
		COALESCE(start_line,0), COALESCE(end_line,0), COALESCE(signature,''), COALESCE(return_type,''),
		language, is_test, COALESCE(parent_id,0)
		FROM nodes WHERE COALESCE(node_type,'') = '' ORDER BY file_path, start_line, id`)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("load nodes for taxonomy: %w", err)
	}
	defer rows.Close()
	var nodes []*store.Node
	var ids []int64
	index := make(map[int64]int)
	for rows.Next() {
		n := &store.Node{}
		if err := rows.Scan(&n.ID, &n.Label, &n.Name, &n.QualifiedName, &n.FilePath,
			&n.StartLine, &n.EndLine, &n.Signature, &n.ReturnType, &n.Language, &n.IsTest, &n.ParentID); err != nil {
			return nil, nil, nil, fmt.Errorf("scan node for taxonomy: %w", err)
		}
		index[n.ID] = len(nodes)
		nodes = append(nodes, n)
		ids = append(ids, n.ID)
	}
	return nodes, ids, index, rows.Err()
}

func loadPropertyRefsTx(tx *sql.Tx, index map[int64]int) ([]parser.PropertyRef, error) {
	rows, err := tx.Query(`SELECT node_id, kind, value, COALESCE(line,0), COALESCE(confidence,1.0) FROM properties ORDER BY id`)
	if err != nil {
		return nil, fmt.Errorf("load properties for taxonomy: %w", err)
	}
	defer rows.Close()
	var out []parser.PropertyRef
	for rows.Next() {
		var nodeID int64
		var p parser.PropertyRef
		if err := rows.Scan(&nodeID, &p.Kind, &p.Value, &p.Line, &p.Confidence); err != nil {
			return nil, fmt.Errorf("scan property for taxonomy: %w", err)
		}
		idx, ok := index[nodeID]
		if !ok {
			continue
		}
		p.NodeIdx = idx
		out = append(out, p)
	}
	return out, rows.Err()
}
