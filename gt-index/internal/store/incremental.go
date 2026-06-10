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
	// EvidenceType carries the ORIGINAL edge's evidence marker (ast_call,
	// name_match, name_match_qualified_unresolved, …). It is the only stored
	// signal that the original call was a qualified stdlib-shadow the full-index
	// resolver already demoted (resolver.go:743-747). The restore MUST preserve
	// it so the incremental (`-file`) path does not re-launder a demoted edge
	// back to CERTIFIED — parity with the full path's qualifiedUnresolved gate.
	EvidenceType string
	// TargetQualifiedName is the freshly-deletable target node's qualified_name,
	// carried for parity with the full resolver index (it reads qualified_name)
	// so the incremental path resolves against a non-lobotomized node view.
	TargetQualifiedName string
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
		        COALESCE(e.resolution_method, ''), COALESCE(e.confidence, 0.0),
		        COALESCE(e.evidence_type, ''), COALESCE(n.qualified_name, '')
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
			&r.ResolutionMethod, &r.Confidence, &r.EvidenceType, &r.TargetQualifiedName); err != nil {
			return nil, fmt.Errorf("scan incoming edge: %w", err)
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// deterministicRestoreMethods is the set of resolution methods the deterministic
// resolver strategies + the offline LSP pass produce (the curation_map set). #B6:
// an incremental restore must PRESERVE these with their original confidence/tier
// — the previous {same_file, import}-only preserve condition stripped every lsp/
// type_flow/inherited/verified_unique/… edge targeting a reindexed file down to a
// name_match guess, so a single `-file` reindex lobotomized the resolved tiers.
var deterministicRestoreMethods = map[string]bool{
	"lsp":             true,
	"lsp_verified":    true,
	"verified_unique": true,
	"type_flow":       true,
	"import_type":     true,
	"inherited":       true,
	"unique_method":   true,
	"return_type":     true,
	"impl_method":     true,
	"same_file":       true,
	"import":          true,
}

// tierForConfidence mirrors resolver.tierFor (CLAUDE.md:222 — the ONE threshold
// table) for the store package, which cannot import resolver (import cycle).
// Keep the thresholds in lockstep with resolver.tierFor.
//
//	conf >= 0.9       -> CERTIFIED
//	0.5 <= conf < 0.9 -> CANDIDATE
//	conf < 0.5        -> SPECULATIVE
func tierForConfidence(conf float64) string {
	if conf >= 0.9 {
		return "CERTIFIED"
	}
	if conf >= 0.5 {
		return "CANDIDATE"
	}
	return "SPECULATIVE"
}

// ResolveIncomingEdgesTx re-resolves the snapshot against freshly-inserted
// nodes in `filePath`. Deterministic-method edges with one candidate are
// preserved verbatim (method + confidence, tier re-derived via the tierFor
// table); only genuinely-unresolvable edges fall to a name_match guess.
// Zero candidates means the symbol was renamed/removed; the edge is dropped
// silently and counted in `unresolved`. Returns (restored, unresolved).
func ResolveIncomingEdgesTx(tx *sql.Tx, snap []IncomingEdgeRef, filePath string) (int, int, error) {
	if len(snap) == 0 {
		return 0, 0, nil
	}
	// #B8c: ORDER BY id so the candidate list (and the ids[0] pick below) is
	// explicitly deterministic, not an accident of SQLite scan order.
	lookup, err := tx.Prepare(`SELECT id FROM nodes WHERE name = ? AND file_path = ? ORDER BY id`)
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

		// PARITY with the full-index resolver's qualifiedUnresolved gate
		// (resolver.go:721,743-747): if the ORIGINAL edge was a qualified
		// stdlib-shadow the resolver already demoted, the incremental restore
		// must NOT re-launder it back to CERTIFIED. The only stored signal of
		// that demotion is the edge's evidence_type marker; an `import`/
		// `same_file` row that still carries it is a laundered legacy edge and
		// is treated as demoted too (correct-or-quiet — never re-promote a guess).
		qualifiedUnresolved := r.EvidenceType == "name_match_qualified_unresolved"

		// #B6: if unambiguous (1 candidate) and the original edge was resolved by
		// ANY deterministic method, preserve method + confidence verbatim and
		// re-derive the tier from the ONE threshold table. Only genuinely-
		// unresolvable edges (ambiguous re-match, original name_match, or the
		// qualified-unresolved stdlib-shadow demote) fall to a name_match guess.
		var conf float64
		var method string
		var tier string
		var evType string
		if !qualifiedUnresolved && len(ids) == 1 && deterministicRestoreMethods[r.ResolutionMethod] {
			conf = r.Confidence
			// Item #4: floor ONLY the literal pre-v14 0.0/NULL sentinel to the
			// method-appropriate verified value (same_file/import → 1.0, the
			// computeConfidence table; the other deterministic methods post-date
			// v14 and can never carry the sentinel). Any conf>0 the pipeline
			// previously stored — including an intentionally-lowered one — is
			// PRESERVED verbatim: never re-certify a deliberately-lowered edge.
			if conf <= 0.0 && (r.ResolutionMethod == "same_file" || r.ResolutionMethod == "import") {
				conf = 1.0
			}
			method = r.ResolutionMethod
			tier = tierForConfidence(conf)
			// Preserve the original evidence marker; fall back to the method-
			// appropriate default for legacy rows that stored none.
			evType = r.EvidenceType
			if evType == "" {
				if method == "same_file" || method == "import" {
					evType = "ast_call"
				} else {
					evType = method
				}
			}
		} else {
			method = "name_match"
			evType = "name_match"
			switch {
			case qualifiedUnresolved:
				// Parity with the resolver demote (resolver.go: conf 0.2,
				// evidence name_match_qualified_unresolved): a demoted stdlib-
				// shadow must restore at demoted confidence, not climb back to
				// 0.9 via the single-candidate row below.
				conf = 0.2
				evType = "name_match_qualified_unresolved"
			case len(ids) == 1:
				// #B6 split-brain fix: this row used to store conf 0.9 with tier
				// SPECULATIVE — tierFor(0.9) is CERTIFIED, and a name_match must
				// NEVER restore as CERTIFIED (name_match is not a fact). Cap the
				// confidence at the 2-candidate ambiguity score so conf and tier
				// agree (0.6 → CANDIDATE): a single-candidate re-match without
				// the original qualifier context is not a verified edge.
				conf = 0.6
			case len(ids) == 2:
				conf = 0.6
			case len(ids) <= 5:
				conf = 0.4
			default:
				conf = 0.2
			}
			tier = tierForConfidence(conf)
		}
		// Pick the first candidate deterministically (id ASC from SELECT).
		// Edge confidence reflects ambiguity across all candidates.
		var srcFile interface{}
		if r.SourceFile == "" {
			srcFile = nil
		} else {
			srcFile = r.SourceFile
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
//
// C5 (decision: Option B — drop-on-incremental). The C7 transitive-closure
// table (sqlite.go) is a FULL-INDEX-ONLY sidecar. It has NO foreign key to
// nodes, so deleting this file's nodes here would otherwise leave closure rows
// whose source_id/target_id point at dead node IDs — silently misattributing
// reach to whatever node later reuses that AUTOINCREMENT id. We do NOT recompute
// the closure on the incremental path (recompute would reintroduce the 29x BFS
// cost C7 deliberately avoided); instead we DROP the affected rows here and let
// the Python reader (graph.py ImportGraph._closure_is_fresh) detect the now-
// partial table via the closure_count marker mismatch and fall back to live BFS
// until the next full index rebuilds the closure.
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

	// C5 — drop the closure rows that reference any node in this file, on EITHER
	// endpoint (a row is orphaned if its source_id OR its target_id is deleted).
	// MUST run before the node delete: the subqueries resolve against the
	// current nodes table, mirroring the edges/properties/assertions order
	// above. The `closure` table is guaranteed present here — store.Open() runs
	// createSchema (CREATE TABLE IF NOT EXISTS closure) on every open, including
	// runIncremental's — so an unconditional DELETE is safe on pre-C7 graph.db
	// too (it simply affects zero rows).
	if _, err := tx.Exec(
		`DELETE FROM closure
		   WHERE source_id IN (SELECT id FROM nodes WHERE file_path = ?)
		      OR target_id IN (SELECT id FROM nodes WHERE file_path = ?)`,
		filePath, filePath,
	); err != nil {
		return 0, 0, fmt.Errorf("delete closure rows for %s: %w", filePath, err)
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
	// Parity with the full-index resolver's node view (resolver.go strategies
	// 1.75/1.94/1.95 read qualified_name, signature, parent_id). The incremental
	// reindex must rebuild the SAME columns, else qualified/self/super (CHA) calls
	// re-resolve against a lobotomized index — the root enabler of the
	// qualified-unresolved re-launder on the `-file` path.
	rows, err := d.db.Query(
		`SELECT id, label, name, COALESCE(qualified_name, ''), file_path,
		        COALESCE(signature, ''), COALESCE(return_type, ''), language, is_test, COALESCE(parent_id, 0)
		   FROM nodes`,
	)
	if err != nil {
		return nil, nil, fmt.Errorf("query all nodes: %w", err)
	}
	defer rows.Close()

	var nodes []Node
	var ids []int64
	for rows.Next() {
		var n Node
		if err := rows.Scan(&n.ID, &n.Label, &n.Name, &n.QualifiedName, &n.FilePath,
			&n.Signature, &n.ReturnType, &n.Language, &n.IsTest, &n.ParentID); err != nil {
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
		`INSERT INTO assertions (test_node_id, target_node_id, resolution_score, kind, expression, expected, line) VALUES (?, ?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		return fmt.Errorf("prepare insert assertions: %w", err)
	}
	defer stmt.Close()
	for i, a := range assertions {
		if _, err := stmt.Exec(a.TestNodeID, a.TargetNodeID, a.ResolutionScore, a.Kind, a.Expression, a.Expected, a.Line); err != nil {
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
