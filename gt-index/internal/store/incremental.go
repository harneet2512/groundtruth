// Package store: incremental file-keyed reindex helpers.
//
// Supports `gt-index -file <relpath>` mode: delete-and-replace a single file's
// nodes and edges in an existing graph.db without rebuilding from scratch.
//
// Contract:
//   - Step 5 (spec): edges are deleted by source_file = ? OR target_id IN
//     (SELECT id FROM nodes WHERE file_path = ?). The schema has no
//     target_file column; targeting flows through target_id → nodes.id.
//     This delete MUST run BEFORE the node delete (the subquery needs the
//     nodes intact).
//   - Step 6: nodes deleted by file_path = ?.
//   - The orphan-edge invariant (edges referencing missing nodes) MUST hold
//     after this operation. Verified by:
//       SELECT COUNT(*) FROM edges
//        WHERE source_id NOT IN (SELECT id FROM nodes)
//           OR target_id NOT IN (SELECT id FROM nodes);
package store

import (
	"database/sql"
	"fmt"
	"os"
	"time"
)

// IncomingEdgeRef is one row of the snapshot taken BEFORE we delete a
// reparsed file's nodes/edges. It carries the minimum needed to re-resolve
// the edge against the freshly-inserted node IDs by name.
type IncomingEdgeRef struct {
	SourceID         int64   // caller node id (lives in some other file — survives the delete)
	SourceLine       int     // line in the source file where the call lived
	EdgeType         string  // "CALLS", etc.
	SourceFile       string  // source file path of the calling edge
	TargetName       string  // name of the target symbol that lived in the file being reparsed
	ResolutionMethod string  // original resolution method (same_file, import, name_match)
	Confidence       float64 // original confidence
}

// SnapshotIncomingEdgesTx captures cross-file edges whose target is a node
// inside `filePath`, before the delete. Self-edges (source_file == filePath)
// are excluded — those will be re-emitted naturally when the file is
// re-parsed and its outgoing calls are re-resolved.
//
// Cap is a defensive upper bound on rows returned; 0 means default 50,000.
func SnapshotIncomingEdgesTx(tx *sql.Tx, filePath string, cap int) ([]IncomingEdgeRef, error) {
	if cap <= 0 {
		cap = 50000
	}
	rows, err := tx.Query(
		`SELECT e.source_id, e.source_line, e.type, COALESCE(e.source_file, ''), n.name,
		        COALESCE(e.resolution_method, ''), COALESCE(e.confidence, 0.0)
		   FROM edges e
		   JOIN nodes n ON e.target_id = n.id
		  WHERE n.file_path = ?
		    AND (e.source_file IS NULL OR e.source_file != ?)
		  LIMIT ?`,
		filePath, filePath, cap,
	)
	if err != nil {
		return nil, fmt.Errorf("snapshot incoming edges for %s: %w", filePath, err)
	}
	defer rows.Close()

	var out []IncomingEdgeRef
	for rows.Next() {
		var r IncomingEdgeRef
		if err := rows.Scan(&r.SourceID, &r.SourceLine, &r.EdgeType, &r.SourceFile, &r.TargetName,
			&r.ResolutionMethod, &r.Confidence); err != nil {
			return nil, fmt.Errorf("scan incoming edge: %w", err)
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// ResolveIncomingEdgesTx re-resolves the snapshot against freshly-inserted
// nodes in `filePath`. Confidence follows the CLAUDE.md name_match table:
// 1 candidate → 0.9, 2 → 0.6, 3-5 → 0.4, 6+ → 0.2. Zero candidates means
// the symbol was renamed/removed; the edge is dropped silently and counted
// in `unresolved`. Returns (restored, unresolved).
func ResolveIncomingEdgesTx(tx *sql.Tx, snap []IncomingEdgeRef, filePath string) (int, int, error) {
	if len(snap) == 0 {
		return 0, 0, nil
	}
	lookup, err := tx.Prepare(`SELECT id FROM nodes WHERE name = ? AND file_path = ?`)
	if err != nil {
		return 0, 0, fmt.Errorf("prepare incoming lookup: %w", err)
	}
	defer lookup.Close()
	ins, err := tx.Prepare(
		`INSERT INTO edges (source_id, target_id, type, source_line, source_file,
		 resolution_method, confidence, metadata, trust_tier, candidate_count, evidence_type, verification_status)
		 VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'unverified')`,
	)
	if err != nil {
		return 0, 0, fmt.Errorf("prepare incoming insert: %w", err)
	}
	defer ins.Close()

	restored, unresolved := 0, 0
	for _, r := range snap {
		rows, err := lookup.Query(r.TargetName, filePath)
		if err != nil {
			return restored, unresolved, fmt.Errorf("lookup %s in %s: %w", r.TargetName, filePath, err)
		}
		var ids []int64
		for rows.Next() {
			var id int64
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				return restored, unresolved, fmt.Errorf("scan target id: %w", err)
			}
			ids = append(ids, id)
		}
		rows.Close()

		if len(ids) == 0 {
			unresolved++
			continue
		}

		// If unambiguous (1 candidate) and original was high-confidence, preserve it
		var conf float64
		var method string
		var tier string
		if len(ids) == 1 && (r.ResolutionMethod == "same_file" || r.ResolutionMethod == "import") {
			conf = r.Confidence
			if conf < 0.5 {
				conf = 1.0 // pre-v14 databases have 0.0 default; restore to verified
			}
			method = r.ResolutionMethod
			tier = "CERTIFIED"
		} else {
			method = "name_match"
			switch {
			case len(ids) == 1:
				conf = 0.9
				tier = "CERTIFIED"
			case len(ids) == 2:
				conf = 0.6
				tier = "CANDIDATE"
			case len(ids) <= 5:
				conf = 0.4
				tier = "SPECULATIVE"
			default:
				conf = 0.2
				tier = "SPECULATIVE"
			}
		}
		// Pick the first candidate deterministically (id ASC from SELECT).
		// Edge confidence reflects ambiguity across all candidates.
		var srcFile interface{}
		if r.SourceFile == "" {
			srcFile = nil
		} else {
			srcFile = r.SourceFile
		}
		evType := method
		if method == "same_file" || method == "import" {
			evType = "ast_call"
		}
		if _, err := ins.Exec(r.SourceID, ids[0], r.EdgeType, r.SourceLine, srcFile,
			method, conf, tier, len(ids), evType); err != nil {
			return restored, unresolved, fmt.Errorf("insert restored edge: %w", err)
		}
		restored++
	}
	return restored, unresolved, nil
}

// DeleteFileEdgesAndNodesTx removes all edges touching `filePath` (as
// source-file or as target node) and then all nodes belonging to it,
// inside the supplied transaction.
//
// Order is enforced: edges first (subquery references nodes), then nodes.
// Returns (edgesDeleted, nodesDeleted).
func DeleteFileEdgesAndNodesTx(tx *sql.Tx, filePath string) (int64, int64, error) {
	// Step 5: delete edges sourced from this file OR targeting any node in this file.
	// NOTE: must run before the node delete; the subquery resolves against the
	// current nodes table.
	resE, err := tx.Exec(
		`DELETE FROM edges
		   WHERE source_file = ?
		      OR target_id IN (SELECT id FROM nodes WHERE file_path = ?)`,
		filePath, filePath,
	)
	if err != nil {
		return 0, 0, fmt.Errorf("delete edges for %s: %w", filePath, err)
	}
	edgesDeleted, _ := resE.RowsAffected()

	// Also delete properties + assertions tied to nodes in this file, so they
	// don't dangle after the node delete. (Not required by the B0 spec, but
	// keeps the DB internally consistent — properties.node_id and
	// assertions.test_node_id reference nodes.id with no ON DELETE CASCADE.)
	if _, err := tx.Exec(
		`DELETE FROM properties WHERE node_id IN (SELECT id FROM nodes WHERE file_path = ?)`,
		filePath,
	); err != nil {
		return 0, 0, fmt.Errorf("delete properties for %s: %w", filePath, err)
	}
	if _, err := tx.Exec(
		`DELETE FROM assertions WHERE test_node_id IN (SELECT id FROM nodes WHERE file_path = ?)`,
		filePath,
	); err != nil {
		return 0, 0, fmt.Errorf("delete assertions for %s: %w", filePath, err)
	}

	// Step 6: delete the nodes themselves.
	resN, err := tx.Exec(`DELETE FROM nodes WHERE file_path = ?`, filePath)
	if err != nil {
		return 0, 0, fmt.Errorf("delete nodes for %s: %w", filePath, err)
	}
	nodesDeleted, _ := resN.RowsAffected()

	return edgesDeleted, nodesDeleted, nil
}

// GetAllNodes returns every node in the DB (id + identifying fields) in
// stable order. Used to rebuild the resolver's name and file indexes during
// an incremental reindex.
//
// We return (nodes, ids) parallel so callers can reuse BuildNameIndex
// unchanged.
func (d *DB) GetAllNodes() ([]Node, []int64, error) {
	rows, err := d.db.Query(
		`SELECT id, label, name, file_path, language, is_test FROM nodes`,
	)
	if err != nil {
		return nil, nil, fmt.Errorf("query all nodes: %w", err)
	}
	defer rows.Close()

	var nodes []Node
	var ids []int64
	for rows.Next() {
		var n Node
		if err := rows.Scan(&n.ID, &n.Label, &n.Name, &n.FilePath, &n.Language, &n.IsTest); err != nil {
			return nil, nil, fmt.Errorf("scan node: %w", err)
		}
		nodes = append(nodes, n)
		ids = append(ids, n.ID)
	}
	return nodes, ids, rows.Err()
}

// GetDistinctFilesAndLanguages returns parallel slices of every distinct
// file path and its language stored in the nodes table. Used to rebuild
// resolver.BuildFileMap during an incremental reindex.
func (d *DB) GetDistinctFilesAndLanguages() ([]string, []string, error) {
	rows, err := d.db.Query(
		`SELECT file_path, language FROM nodes GROUP BY file_path`,
	)
	if err != nil {
		return nil, nil, fmt.Errorf("query distinct files: %w", err)
	}
	defer rows.Close()

	var paths, langs []string
	for rows.Next() {
		var p, l string
		if err := rows.Scan(&p, &l); err != nil {
			return nil, nil, fmt.Errorf("scan file: %w", err)
		}
		paths = append(paths, p)
		langs = append(langs, l)
	}
	return paths, langs, rows.Err()
}

// FileExists reports whether the DB has any rows for the given file path.
func (d *DB) FileExists(filePath string) bool {
	var n int
	d.db.QueryRow(`SELECT COUNT(*) FROM nodes WHERE file_path = ?`, filePath).Scan(&n)
	return n > 0
}

// ──────────────────────────────────────────────────────────────────────────
// Transaction-scoped insert helpers, used by the incremental reindex path
// so that the spec's "BEGIN ... COMMIT" wraps all of steps 5–9 atomically.
// They mirror the existing BatchInsertNodes / BatchInsertEdges / InsertFileHash
// helpers but accept an *sql.Tx supplied by the caller.
// ──────────────────────────────────────────────────────────────────────────

// BatchInsertNodesTx inserts nodes inside the given tx. Returns the
// auto-generated IDs in input order.
func BatchInsertNodesTx(tx *sql.Tx, nodes []*Node) ([]int64, error) {
	if len(nodes) == 0 {
		return nil, nil
	}
	stmt, err := tx.Prepare(
		`INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line,
		 signature, return_type, is_exported, is_test, language, parent_id)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		return nil, fmt.Errorf("prepare insert nodes: %w", err)
	}
	defer stmt.Close()

	ids := make([]int64, len(nodes))
	for i, n := range nodes {
		res, err := stmt.Exec(
			n.Label, n.Name, n.QualifiedName, n.FilePath, n.StartLine, n.EndLine,
			n.Signature, n.ReturnType, n.IsExported, n.IsTest, n.Language, n.ParentID,
		)
		if err != nil {
			return nil, fmt.Errorf("insert node %d: %w", i, err)
		}
		id, err := res.LastInsertId()
		if err != nil {
			return nil, fmt.Errorf("last insert id %d: %w", i, err)
		}
		ids[i] = id
	}
	return ids, nil
}

// BatchInsertEdgesTx inserts edges inside the given tx.
func BatchInsertEdgesTx(tx *sql.Tx, edges []*Edge) error {
	if len(edges) == 0 {
		return nil
	}
	stmt, err := tx.Prepare(
		`INSERT INTO edges (source_id, target_id, type, source_line, source_file,
		 resolution_method, confidence, metadata, trust_tier, candidate_count, evidence_type, verification_status)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		return fmt.Errorf("prepare insert edges: %w", err)
	}
	defer stmt.Close()

	for i, e := range edges {
		if _, err := stmt.Exec(
			e.SourceID, e.TargetID, e.Type, e.SourceLine, e.SourceFile,
			e.ResolutionMethod, e.Confidence, e.Metadata,
			e.TrustTier, e.CandidateCount, e.EvidenceType, e.VerificationStatus,
		); err != nil {
			return fmt.Errorf("insert edge %d: %w", i, err)
		}
	}
	return nil
}

// BatchInsertPropertiesTx inserts properties inside the given tx.
func BatchInsertPropertiesTx(tx *sql.Tx, props []*Property) error {
	if len(props) == 0 {
		return nil
	}
	stmt, err := tx.Prepare(
		`INSERT INTO properties (node_id, kind, value, line, confidence) VALUES (?, ?, ?, ?, ?)`,
	)
	if err != nil {
		return fmt.Errorf("prepare insert properties: %w", err)
	}
	defer stmt.Close()
	for i, p := range props {
		if _, err := stmt.Exec(p.NodeID, p.Kind, p.Value, p.Line, p.Confidence); err != nil {
			return fmt.Errorf("insert property %d: %w", i, err)
		}
	}
	return nil
}

// BatchInsertAssertionsTx inserts assertions inside the given tx.
func BatchInsertAssertionsTx(tx *sql.Tx, assertions []*Assertion) error {
	if len(assertions) == 0 {
		return nil
	}
	stmt, err := tx.Prepare(
		`INSERT INTO assertions (test_node_id, target_node_id, kind, expression, expected, line) VALUES (?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		return fmt.Errorf("prepare insert assertions: %w", err)
	}
	defer stmt.Close()
	for i, a := range assertions {
		if _, err := stmt.Exec(a.TestNodeID, a.TargetNodeID, a.Kind, a.Expression, a.Expected, a.Line); err != nil {
			return fmt.Errorf("insert assertion %d: %w", i, err)
		}
	}
	return nil
}

// InsertFileHashTx records a file's content hash inside the given tx.
func InsertFileHashTx(tx *sql.Tx, filePath, hash, language string) error {
	ts := os.Getenv("GT_INDEX_FIXED_TS")
	if ts == "" {
		ts = time.Now().UTC().Format(time.RFC3339)
	}
	_, err := tx.Exec(
		`INSERT OR REPLACE INTO file_hashes (file_path, content_hash, language, indexed_at) VALUES (?, ?, ?, ?)`,
		filePath, hash, language, ts,
	)
	return err
}

// UpdateParentIDTx sets the parent_id for a node inside the given tx.
func UpdateParentIDTx(tx *sql.Tx, nodeID, parentID int64) error {
	_, err := tx.Exec(`UPDATE nodes SET parent_id = ? WHERE id = ?`, parentID, nodeID)
	return err
}
