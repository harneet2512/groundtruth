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
//     SELECT COUNT(*) FROM edges
//     WHERE source_id NOT IN (SELECT id FROM nodes)
//     OR target_id NOT IN (SELECT id FROM nodes);
package store

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
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
// PROMOTED DEPTH EDGES ARE EXCLUDED (resolution_method LIKE 'promote_%').
// The promote pass (resolver.PromotePropertyEdges, Pass 4f) regenerates EVERY
// promoted edge whole-graph AFTER the incremental tx commits (main.go Step 10.5),
// and that DELETE-before-rebuild pass is idempotent only over the `promote_%`
// resolution_method namespace. If an INCOMING promoted depth edge (e.g. another
// file's method READS/WRITES a class in this reindexed file) were snapshotted
// here, it would be deleted by DeleteFileEdgesAndNodesTx (target_id-keyed) and
// then RESTORED by ResolveIncomingEdgesTx as a phantom `name_match`-tier edge —
// which the post-commit promote DELETE cannot reach (it only matches 'promote_%').
// The promote pass would then re-emit the genuine `promote_*` edge alongside the
// phantom → a DUPLICATE the idempotence contract can never converge (edges has no
// UNIQUE constraint). Excluding promoted edges from the snapshot is the clean fix:
// they are never restored as guesses; the whole-graph promote pass owns them.
//
// Cap is a defensive upper bound on rows returned; 0 means default 50,000.
//
// Two additional guards keep the snapshot honest on the resolution-v2 schema:
//   - `e.source_id NOT IN (F's nodes)` — the denormalized source_file column is
//     '' on structural fact links (factLinkSQL) and can drift on taxonomy rows;
//     membership in the about-to-be-deleted node set is the authoritative
//     "this edge dies with the file" test. Without it, a file-local edge whose
//     source_file is blank would be snapshotted and then restored onto a
//     deleted source id — a manufactured orphan.
//   - `e.type NOT IN (resolution overlay types)` — CANDIDATE_TARGET and
//     SELECTED_TARGET edges into the reparsed file are snapshot/rebound by the
//     v2 path (SnapshotIncomingV2EdgesTx + RebindIncomingV2EdgesTx), which
//     rebinds them in place against the surviving callsite nodes instead of
//     re-deriving them as name_match guesses. HAS_*/HAS_CALLSITE rows can never
//     be cross-file into F (their source is F's own callsite/symbol), so they
//     are excluded here and die with F's overlay.
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
		    AND e.source_id NOT IN (SELECT id FROM nodes WHERE file_path = ?)
		    AND e.type NOT IN ('HAS_CALLSITE','CANDIDATE','CANDIDATE_TARGET','SELECTED_TARGET',
		                       'HAS_DERIVATION_FACT','HAS_COMPLETENESS_FACT','HAS_UNRESOLVED_FACT')
		    AND (e.resolution_method IS NULL OR e.resolution_method NOT LIKE 'promote_%')
		  LIMIT ?`,
		filePath, filePath, filePath, cap,
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
	"callable_value":  true,
	"same_file":       true,
	"import":          true,
}

// typeOrUniquenessDerivedMethods are the deterministic methods whose tier was earned
// by a fact a BARE NAME does not re-prove: a receiver TYPE (type_flow/import_type/
// inherited/impl_method/unique_method/return_type) or GLOBAL UNIQUENESS at index time
// (verified_unique). When an incremental restore can only re-match by bare name (no
// surviving node whose qualified_name equals the original target's), the original
// receiver-type / uniqueness context is GONE — re-stamping the original CERTIFIED tier
// onto whatever single same-named node now occupies the file would launder the tier
// onto a possibly-wrong target. These restore CAPPED at CANDIDATE (0.6). The
// signature-derived `same_file`/`import` methods are NOT here: their proof (the call
// is in the same file / the file imports the name) survives a bare-name re-match.
var typeOrUniquenessDerivedMethods = map[string]bool{
	"verified_unique": true,
	"type_flow":       true,
	"import_type":     true,
	"inherited":       true,
	"impl_method":     true,
	"unique_method":   true,
	"return_type":     true,
	// callable_value is binding-derived (an alias write or a formal
	// parameter): a bare-name re-match cannot re-prove the tracked value, so
	// its tier is capped like the type-derived methods.
	"callable_value": true,
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
// and counted in `unresolved`. When a GraphCompletionIdentity is supplied
// (variadic, optional — older callers and tests pass none) the drop is ALSO
// recorded as an UnresolvedFact node linked from the surviving source symbol
// by HAS_UNRESOLVED_FACT, so a lost call edge is a recorded fact rather than
// silence — the same honesty contract RebindIncomingV2EdgesTx applies to the
// v2 edge family. Returns (restored, unresolved).
func ResolveIncomingEdgesTx(tx *sql.Tx, snap []IncomingEdgeRef, filePath string, identity ...GraphCompletionIdentity) (int, int, error) {
	if len(snap) == 0 {
		return 0, 0, nil
	}
	// #B8c: ORDER BY id so the candidate list (and the ids[0] pick below) is
	// explicitly deterministic, not an accident of SQLite scan order. Also select
	// qualified_name so the restore can re-prove TARGET IDENTITY against the original
	// edge's TargetQualifiedName (P0: a bare-name re-match must not launder a verified
	// tier onto a different node that happens to share the simple name).
	lookup, err := tx.Prepare(`SELECT id, COALESCE(qualified_name, '') FROM nodes WHERE name = ? AND file_path = ? ORDER BY id`)
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
		// qnameMatchID is the candidate whose qualified_name EXACTLY equals the
		// original edge's TargetQualifiedName — the node that re-proves target
		// identity (not merely the simple name). 0 = no exact-qname candidate.
		var qnameMatchID int64
		for rows.Next() {
			var id int64
			var qname string
			if err := rows.Scan(&id, &qname); err != nil {
				rows.Close()
				return restored, unresolved, fmt.Errorf("scan target id: %w", err)
			}
			ids = append(ids, id)
			if qnameMatchID == 0 && r.TargetQualifiedName != "" && qname == r.TargetQualifiedName {
				qnameMatchID = id
			}
		}
		rows.Close()

		if len(ids) == 0 {
			unresolved++
			if len(identity) > 0 && identity[0].BuildID != "" {
				if err := recordUnresolvedIncomingTx(tx, r, filePath, identity[0]); err != nil {
					return restored, unresolved, err
				}
			}
			continue
		}

		// Target node for the restored edge: the exact-qualified-name match when one
		// exists (identity re-proven), else the deterministic first candidate (id ASC).
		// qnameMatched gates whether a type/uniqueness-derived tier may be PRESERVED:
		// without an exact-qname re-match the bare name does not re-prove the original
		// receiver-type / global-uniqueness fact, so that tier is capped at CANDIDATE.
		targetID := ids[0]
		qnameMatched := false
		if qnameMatchID != 0 {
			targetID = qnameMatchID
			qnameMatched = true
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
			// P0 DEMOTE: a type/uniqueness-derived tier (verified_unique/type_flow/
			// import_type/inherited/impl_method/unique_method/return_type) was earned
			// by a receiver TYPE or GLOBAL UNIQUENESS that a bare-name re-match does
			// NOT re-prove. If we could not re-prove target identity via an exact
			// qualified_name match, the single surviving same-named node may be a
			// DIFFERENT symbol (rename-and-replace) — preserving the CERTIFIED tier
			// would launder it onto the wrong target. Cap at CANDIDATE (0.6) and let
			// the tier re-derive to CANDIDATE. The signature-derived same_file/import
			// methods are exempt: their proof survives a bare-name re-match.
			if !qnameMatched && typeOrUniquenessDerivedMethods[method] && conf > 0.6 {
				conf = 0.6
			}
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
		// Target is the exact-qualified-name match when one survived, else the
		// deterministic first candidate (id ASC). Edge confidence reflects ambiguity
		// across all candidates (candidate_count = len(ids)).
		var srcFile interface{}
		if r.SourceFile == "" {
			srcFile = nil
		} else {
			srcFile = r.SourceFile
		}
		if _, err := ins.Exec(r.SourceID, targetID, r.EdgeType, r.SourceLine, srcFile,
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
	//
	// The delete is symmetric on node membership, not just the denormalized
	// source_file column: the resolution-v2 fact links (HAS_DERIVATION_FACT /
	// HAS_COMPLETENESS_FACT / HAS_UNRESOLVED_FACT) and taxonomy rows are stamped
	// with source_file='' even though their source node lives in this file, so
	// `source_id IN (file's nodes)` is the only predicate that reliably retires
	// the reparsed file's OWN callsite/fact overlay.
	//
	// The NOT-carve-out preserves cross-file resolution-v2 edges INTO the file
	// (CANDIDATE_TARGET / SELECTED_TARGET whose source is a surviving callsite
	// node in another file). They were captured by SnapshotIncomingV2EdgesTx and
	// are rebound in place by RebindIncomingV2EdgesTx once the new node ids
	// exist; deleting them here would force a re-insert that cannot reproduce
	// their derivation columns, and leaving them to dangle is the orphan bug
	// this path exists to close. The same file's OWN callsite-sourced v2 edges
	// still die: their source_id IS in this file's node set.
	resE, err := tx.Exec(
		`DELETE FROM edges
		   WHERE (source_file = ?
		      OR source_id IN (SELECT id FROM nodes WHERE file_path = ?)
		      OR target_id IN (SELECT id FROM nodes WHERE file_path = ?))
		     AND NOT (type IN ('CANDIDATE_TARGET','SELECTED_TARGET')
		              AND source_id NOT IN (SELECT id FROM nodes WHERE file_path = ?)
		              AND target_id IN (SELECT id FROM nodes WHERE file_path = ?))`,
		filePath, filePath, filePath, filePath, filePath,
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

	// HAR-90 items 5-8: retire the reparsed file's CFG sidecar rows. They are
	// keyed on node_id, so the same file-membership subquery applies — and the
	// delete must precede the node delete below for it to resolve. createSchema
	// guarantees the tables exist on every open, including incremental ones.
	for _, table := range []string{"cfg_blocks", "cfg_edges", "cfg_defs", "cfg_uses"} {
		if _, err := tx.Exec(
			fmt.Sprintf(`DELETE FROM %s WHERE node_id IN (SELECT id FROM nodes WHERE file_path = ?)`, table),
			filePath,
		); err != nil {
			return 0, 0, fmt.Errorf("delete %s rows for %s: %w", table, filePath, err)
		}
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

// ──────────────────────────────────────────────────────────────────────────
// Resolution-v2 incoming-edge rebind.
//
// A full build attaches every callsite's evidence as graph rows: a Callsite
// node per call, HAS_CALLSITE from the caller symbol, CANDIDATE_TARGET edges
// from the callsite to each viable target, at most one SELECTED_TARGET, and
// HAS_*_FACT links to per-callsite fact nodes. The incremental `-file` path
// cannot re-derive other files' callsites (no re-analysis runs), so when a
// reparsed file's symbols are deleted and re-inserted under new row ids, the
// SURVIVING callsites' v2 edges into that file must be rebound in place —
// target_id is a row id, but target_symbol_id is the content-derived stable
// identity (StableResolutionSymbolID), which survives a delete/reinsert of an
// unchanged symbol.
//
// Rebind contract (full-build parity):
//   - exact:    a new node recomputes to the same stable id → target_id only.
//   - moved:    stable id changed (line shift) but (label, qualified_name) —
//               or (label, name) when unqualified — matches exactly one new
//               node → rebind and rewrite the ids that embed the old stable id
//               (edge stable_id, target_symbol_id, derivation_fact_ids, the
//               materialized derivation_fact node, the callsite's
//               derivation_set_id / selected_target_id, and the stable-id
//               lists inside pass_coverage / completeness fact payloads).
//   - lost:     no unique identity match → the edge is DELETED (never left
//               dangling), the owning callsite's candidate_count_v2 /
//               derivation_set_id / candidate ordinals are recomputed, a lost
//               selection clears selected_target_id and transitions
//               candidate_state to 'ambiguous' (candidates remain) or 'empty'
//               (none remain), and an UnresolvedFact node + HAS_UNRESOLVED_FACT
//               link is attached so the dropped resolution is a recorded fact,
//               not silence.
// ──────────────────────────────────────────────────────────────────────────

// IncomingV2EdgeRef is one cross-file resolution-v2 edge (CANDIDATE_TARGET or
// SELECTED_TARGET) whose target was a node inside the reparsed file. The edge
// row itself survives the file's delete (DeleteFileEdgesAndNodesTx carves it
// out); the snapshot exists to capture the OLD target node's identity fields —
// needed for the moved-symbol fallback — before the node delete removes them.
type IncomingV2EdgeRef struct {
	EdgeID            int64  // edges.id — the row rebound in place
	CallsiteNodeID    int64  // source_id — surviving callsite node in another file
	Type              string // CANDIDATE_TARGET | SELECTED_TARGET
	OldTargetID       int64
	CallsiteStableID  string // edges.callsite_stable_id — owning callsite's stable identity
	OldTargetStableID string // edges.target_symbol_id — the symbol identity the edge claims
	EdgeStableID      string // edges.stable_id
	SelectionRuleID   string // edges.selection_rule_id (SELECTED_TARGET only)
	Ordinal           int    // edges.ordinal (CANDIDATE_TARGET; -1 on SELECTED_TARGET)
	PassKind          string // edges.pass_kind
	DerivationFactIDs string // edges.derivation_fact_ids (JSON array)
	DeclaredScope     string
	ReceiverType      string
	ReceiverOrigin    string
	ImportChain       string
	// Old target node identity, captured pre-delete for the moved-symbol
	// fallback match.
	OldName          string
	OldQualifiedName string
	OldLabel         string
	OldStartLine     int
	OldEndLine       int
	OldLanguage      string
}

// SnapshotIncomingV2EdgesTx captures the cross-file CANDIDATE_TARGET /
// SELECTED_TARGET edges whose targets live in `filePath`, before the file's
// nodes are deleted. Rows are returned in edges.id order so the rebind pass is
// deterministic. Edges sourced at the file's OWN callsite nodes are excluded:
// those die with the file's overlay and must never be rebound.
func SnapshotIncomingV2EdgesTx(tx *sql.Tx, filePath string) ([]IncomingV2EdgeRef, error) {
	rows, err := tx.Query(
		`SELECT e.id, e.source_id, e.type, e.target_id,
		        COALESCE(e.callsite_stable_id,''), COALESCE(e.target_symbol_id,''),
		        COALESCE(e.stable_id,''), COALESCE(e.selection_rule_id,''),
		        COALESCE(e.ordinal,-1), COALESCE(e.pass_kind,''),
		        COALESCE(e.derivation_fact_ids,'[]'), COALESCE(e.declared_scope,''),
		        COALESCE(e.receiver_type,''), COALESCE(e.receiver_origin,''),
		        COALESCE(e.import_chain,''),
		        n.name, COALESCE(n.qualified_name,''), n.label,
		        COALESCE(n.start_line,0), COALESCE(n.end_line,0), COALESCE(n.language,'')
		   FROM edges e
		   JOIN nodes n ON e.target_id = n.id
		  WHERE n.file_path = ?
		    AND e.type IN ('CANDIDATE_TARGET','SELECTED_TARGET')
		    AND e.source_id NOT IN (SELECT id FROM nodes WHERE file_path = ?)
		  ORDER BY e.id`,
		filePath, filePath,
	)
	if err != nil {
		return nil, fmt.Errorf("snapshot incoming v2 edges for %s: %w", filePath, err)
	}
	defer rows.Close()

	var out []IncomingV2EdgeRef
	for rows.Next() {
		var r IncomingV2EdgeRef
		if err := rows.Scan(&r.EdgeID, &r.CallsiteNodeID, &r.Type, &r.OldTargetID,
			&r.CallsiteStableID, &r.OldTargetStableID, &r.EdgeStableID, &r.SelectionRuleID,
			&r.Ordinal, &r.PassKind, &r.DerivationFactIDs, &r.DeclaredScope,
			&r.ReceiverType, &r.ReceiverOrigin, &r.ImportChain,
			&r.OldName, &r.OldQualifiedName, &r.OldLabel, &r.OldStartLine, &r.OldEndLine, &r.OldLanguage); err != nil {
			return nil, fmt.Errorf("scan incoming v2 edge: %w", err)
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// rebindV2FactID recomputes a candidate's derivation fact identity after its
// target's stable id changed — the same formula prepareResolutionV2 uses
// (callsiteID, targetStableID, passKind, "1", ordinal, payload).
func rebindV2FactID(ref IncomingV2EdgeRef, newTargetStableID string) string {
	payload := ref.DeclaredScope + "\x00" + ref.ReceiverType + "\x00" + ref.ReceiverOrigin + "\x00" + ref.ImportChain
	return canonicalResolutionID(ref.CallsiteStableID, newTargetStableID, ref.PassKind, "1", strconv.Itoa(ref.Ordinal), payload)
}

// rebindV2EdgeStableID recomputes the edge's own stable id after a moved
// rebind — stableCandidateV2ID for CANDIDATE_TARGET, the SELECTED_TARGET
// formula from insertResolutionV2FactsTx otherwise.
func rebindV2EdgeStableID(ref IncomingV2EdgeRef, newTargetStableID string) string {
	if ref.Type == "SELECTED_TARGET" {
		return canonicalResolutionID(ref.CallsiteStableID, "SELECTED_TARGET", newTargetStableID, ref.SelectionRuleID)
	}
	return stableCandidateV2ID(ref.CallsiteStableID, newTargetStableID)
}

// rewriteStableIDListJSON returns the JSON string array with each element
// mapped through rewrite (or dropped when the rewrite maps to "").
func rewriteStableIDListJSON(raw string, rewrite map[string]string) (string, error) {
	if raw == "" {
		return raw, nil
	}
	var ids []string
	if err := json.Unmarshal([]byte(raw), &ids); err != nil {
		return "", err
	}
	out := make([]string, 0, len(ids))
	for _, id := range ids {
		if next, ok := rewrite[id]; ok {
			if next == "" {
				continue
			}
			out = append(out, next)
			continue
		}
		out = append(out, id)
	}
	return mustJSON(out), nil
}

// RebindIncomingV2EdgesTx rebinds the snapshotted cross-file v2 edges onto the
// reparsed file's freshly inserted nodes, in place. `newNodes`/`newIDs` are
// the freshly inserted nodes and their row ids (parallel slices, as returned
// by BatchInsertNodesTx). `identity` stamps any UnresolvedFact the rebind has
// to record. Returns (rebound, dropped).
func RebindIncomingV2EdgesTx(tx *sql.Tx, snap []IncomingV2EdgeRef, filePath string, newNodes []*Node, newIDs []int64, identity GraphCompletionIdentity) (int, int, error) {
	if len(snap) == 0 {
		return 0, 0, nil
	}

	// Identity indexes over the freshly inserted nodes. A reparse produces the
	// same node set shape the full build produced, so the match keys are the
	// same ones the resolver evidence was derived from.
	byStable := make(map[string][]int64, len(newNodes))
	byQual := make(map[string][]int64)
	byName := make(map[string][]int64)
	newStableByID := make(map[int64]string, len(newNodes))
	for i, n := range newNodes {
		if i >= len(newIDs) || newIDs[i] <= 0 {
			continue
		}
		stable := StableResolutionSymbolID(*n)
		newStableByID[newIDs[i]] = stable
		byStable[stable] = append(byStable[stable], newIDs[i])
		if n.QualifiedName != "" {
			key := n.Label + "\x00" + n.QualifiedName
			byQual[key] = append(byQual[key], newIDs[i])
		}
		key := n.Label + "\x00" + n.Name
		byName[key] = append(byName[key], newIDs[i])
	}

	updEdge, err := tx.Prepare(`UPDATE edges SET target_id=? WHERE id=?`)
	if err != nil {
		return 0, 0, fmt.Errorf("prepare v2 rebind: %w", err)
	}
	defer updEdge.Close()
	updEdgeIDs, err := tx.Prepare(`UPDATE edges SET target_symbol_id=?, stable_id=?, derivation_fact_ids=? WHERE id=?`)
	if err != nil {
		return 0, 0, fmt.Errorf("prepare v2 identity rewrite: %w", err)
	}
	defer updEdgeIDs.Close()
	delEdge, err := tx.Prepare(`DELETE FROM edges WHERE id=?`)
	if err != nil {
		return 0, 0, fmt.Errorf("prepare v2 drop: %w", err)
	}
	defer delEdge.Close()
	updFactNode, err := tx.Prepare(`UPDATE nodes SET stable_id=?, target_symbol_id=? WHERE node_type='derivation_fact' AND stable_id=?`)
	if err != nil {
		return 0, 0, fmt.Errorf("prepare derivation fact rebind: %w", err)
	}
	defer updFactNode.Close()

	// perCallsite accumulates what happened to each surviving callsite so the
	// bookkeeping pass can recompute its derived columns once.
	type callsiteDelta struct {
		nodeID     int64
		passKind   string
		rewrites   map[string]string // old target stable id -> new stable id
		droppedIDs map[string]bool   // old target stable ids that could not be rebound
		lostSelect bool              // this callsite's SELECTED_TARGET was dropped
	}
	deltas := make(map[string]*callsiteDelta)
	deltaFor := func(ref IncomingV2EdgeRef) *callsiteDelta {
		d, ok := deltas[ref.CallsiteStableID]
		if !ok {
			d = &callsiteDelta{nodeID: ref.CallsiteNodeID, passKind: ref.PassKind,
				rewrites: make(map[string]string), droppedIDs: make(map[string]bool)}
			deltas[ref.CallsiteStableID] = d
		}
		if d.passKind == "" {
			d.passKind = ref.PassKind
		}
		return d
	}
	// A callsite may not publish two CANDIDATE_TARGET edges to the same target;
	// when two distinct old symbols collapse onto one new node the second
	// candidate edge is a duplicate the attach contract forbids — drop it as
	// unresolved instead. The dedup is scoped to CANDIDATE_TARGET rows ONLY: a
	// SELECTED_TARGET edge to the same node is not a duplicate of the
	// candidate — it is the selection OF that candidate, and a callsite that
	// survives a reparse legitimately carries both rows onto one target.
	boundTarget := make(map[string]map[int64]bool)

	rebound, dropped := 0, 0
	for _, ref := range snap {
		d := deltaFor(ref)
		var candidates []int64
		if ref.OldTargetStableID != "" {
			candidates = byStable[ref.OldTargetStableID]
		}
		if len(candidates) == 0 && ref.OldQualifiedName != "" {
			candidates = byQual[ref.OldLabel+"\x00"+ref.OldQualifiedName]
		}
		if len(candidates) == 0 {
			candidates = byName[ref.OldLabel+"\x00"+ref.OldName]
		}
		isCandidate := ref.Type == "CANDIDATE_TARGET"
		if len(candidates) != 1 || (isCandidate && boundTarget[ref.CallsiteStableID][candidates[0]]) {
			if _, err := delEdge.Exec(ref.EdgeID); err != nil {
				return rebound, dropped, fmt.Errorf("drop unbindable v2 edge %d: %w", ref.EdgeID, err)
			}
			d.droppedIDs[ref.OldTargetStableID] = true
			if ref.Type == "SELECTED_TARGET" {
				d.lostSelect = true
			}
			dropped++
			continue
		}
		newTarget := candidates[0]
		if isCandidate {
			if boundTarget[ref.CallsiteStableID] == nil {
				boundTarget[ref.CallsiteStableID] = make(map[int64]bool)
			}
			boundTarget[ref.CallsiteStableID][newTarget] = true
		}
		if _, err := updEdge.Exec(newTarget, ref.EdgeID); err != nil {
			return rebound, dropped, fmt.Errorf("rebind v2 edge %d: %w", ref.EdgeID, err)
		}
		newStable := newStableByID[newTarget]
		if newStable != ref.OldTargetStableID {
			newFactID := rebindV2FactID(ref, newStable)
			if _, err := updEdgeIDs.Exec(newStable, rebindV2EdgeStableID(ref, newStable), mustJSON([]string{newFactID}), ref.EdgeID); err != nil {
				return rebound, dropped, fmt.Errorf("rewrite v2 edge %d identity: %w", ref.EdgeID, err)
			}
			var oldFactIDs []string
			if err := json.Unmarshal([]byte(ref.DerivationFactIDs), &oldFactIDs); err == nil {
				for _, oldFactID := range oldFactIDs {
					if _, err := updFactNode.Exec(newFactID, newStable, oldFactID); err != nil {
						return rebound, dropped, fmt.Errorf("rebind derivation fact %s: %w", oldFactID, err)
					}
				}
			}
			if ref.OldTargetStableID != "" {
				d.rewrites[ref.OldTargetStableID] = newStable
			}
		}
		rebound++
	}

	// Bookkeeping pass over every surviving callsite the reparse touched:
	// recompute candidate counts/ordinals and derivation_set_id from the
	// remaining edges, apply selection loss, and attach an UnresolvedFact when
	// the callsite's published resolution changed underneath it.
	callsiteIDs := make([]string, 0, len(deltas))
	for id := range deltas {
		callsiteIDs = append(callsiteIDs, id)
	}
	sort.Strings(callsiteIDs)
	for _, callsiteStableID := range callsiteIDs {
		d := deltas[callsiteStableID]
		if err := reconcileCallsiteAfterRebindTx(tx, callsiteStableID, d.nodeID, d.passKind, d.rewrites, d.droppedIDs, d.lostSelect, identity); err != nil {
			return rebound, dropped, err
		}
	}
	return rebound, dropped, nil
}

// reconcileCallsiteAfterRebindTx rewrites one surviving callsite's derived
// state after its edges into a reparsed file were rebound or dropped. Every
// column it touches is a deterministic function of the remaining edge set —
// the same values a full re-derivation would publish.
func reconcileCallsiteAfterRebindTx(tx *sql.Tx, callsiteStableID string, callsiteNodeID int64, passKind string, rewrites map[string]string, dropped map[string]bool, lostSelect bool, identity GraphCompletionIdentity) error {
	// Renumber the remaining candidates densely (attach contract: ordinals are
	// 0..n-1) and recount the published candidate set.
	type candRow struct {
		id      int64
		factIDs string
	}
	candRows := []candRow{}
	rows, err := tx.Query(`SELECT id, COALESCE(derivation_fact_ids,'[]') FROM edges
		WHERE callsite_stable_id=? AND type='CANDIDATE_TARGET' ORDER BY ordinal, id`, callsiteStableID)
	if err != nil {
		return fmt.Errorf("list surviving candidates for %s: %w", callsiteStableID, err)
	}
	for rows.Next() {
		var r candRow
		if err := rows.Scan(&r.id, &r.factIDs); err != nil {
			rows.Close()
			return fmt.Errorf("scan surviving candidate: %w", err)
		}
		candRows = append(candRows, r)
	}
	if err := rows.Close(); err != nil {
		return err
	}
	factIDs := make([]string, 0, len(candRows))
	for ordinal, r := range candRows {
		if _, err := tx.Exec(`UPDATE edges SET ordinal=?, candidate_count=?, sibling_count=? WHERE id=?`,
			ordinal, len(candRows), len(candRows), r.id); err != nil {
			return fmt.Errorf("renumber candidate edge %d: %w", r.id, err)
		}
		var ids []string
		if err := json.Unmarshal([]byte(r.factIDs), &ids); err == nil {
			factIDs = append(factIDs, ids...)
		}
	}
	if _, err := tx.Exec(`UPDATE edges SET candidate_count=?, sibling_count=? WHERE type='HAS_CALLSITE' AND callsite_stable_id=?`,
		len(candRows), len(candRows), callsiteStableID); err != nil {
		return fmt.Errorf("recount has_callsite for %s: %w", callsiteStableID, err)
	}

	var candidateState, selectedTargetID string
	if err := tx.QueryRow(`SELECT COALESCE(candidate_state,''), COALESCE(selected_target_id,'')
		FROM nodes WHERE id=? AND node_type='callsite'`, callsiteNodeID).
		Scan(&candidateState, &selectedTargetID); err != nil {
		return fmt.Errorf("read callsite %s state: %w", callsiteStableID, err)
	}
	if next, ok := rewrites[selectedTargetID]; ok && next != "" {
		if _, err := tx.Exec(`UPDATE nodes SET selected_target_id=? WHERE id=?`, next, callsiteNodeID); err != nil {
			return fmt.Errorf("rebind selected target for %s: %w", callsiteStableID, err)
		}
		selectedTargetID = next
	}

	newDerivationSet := canonicalResolutionID(factIDs...)
	if _, err := tx.Exec(`UPDATE nodes SET candidate_count_v2=?, derivation_set_id=? WHERE id=?`,
		len(candRows), newDerivationSet, callsiteNodeID); err != nil {
		return fmt.Errorf("update callsite %s derived ids: %w", callsiteStableID, err)
	}

	// Rewrite the stable-id lists embedded in this callsite's pass coverage and
	// completeness facts: moved candidates substitute, dropped ones are removed.
	if err := rewriteCallsiteStableListsTx(tx, callsiteStableID, callsiteNodeID, rewrites, dropped); err != nil {
		return err
	}

	// Selection loss / emptying is a change in the callsite's published state —
	// record it as an unresolved fact on the callsite rather than letting the
	// dropped edge pass silently.
	lostEverything := len(candRows) == 0 && len(dropped) > 0
	if lostSelect || lostEverything {
		newState := "ambiguous"
		reason := "ambiguous_viable_set"
		if len(candRows) == 0 {
			newState = "empty"
			reason = "no_viable_target"
		}
		if _, err := tx.Exec(`UPDATE nodes SET candidate_state=?, selected_target_id=NULL WHERE id=?`,
			newState, callsiteNodeID); err != nil {
			return fmt.Errorf("transition callsite %s: %w", callsiteStableID, err)
		}
		if err := attachUnresolvedFactTx(tx, callsiteStableID, callsiteNodeID, passKind, reason, len(candRows), identity); err != nil {
			return err
		}
	} else if len(dropped) > 0 && candidateState == "selected" && dropped[selectedTargetID] {
		// The candidate edge for the selected target was dropped without a
		// SELECTED_TARGET row (inconsistent legacy graph) — treat as selection loss.
		if _, err := tx.Exec(`UPDATE nodes SET candidate_state='ambiguous', selected_target_id=NULL WHERE id=?`,
			callsiteNodeID); err != nil {
			return fmt.Errorf("transition callsite %s: %w", callsiteStableID, err)
		}
		if err := attachUnresolvedFactTx(tx, callsiteStableID, callsiteNodeID, passKind, "ambiguous_viable_set", len(candRows), identity); err != nil {
			return err
		}
	}
	return nil
}

// rewriteCallsiteStableListsTx substitutes (or removes) target stable ids
// inside the JSON id-list payloads a callsite's evidence carries: the
// HAS_CALLSITE edge's pass_coverage and each completeness_fact node's id
// columns.
func rewriteCallsiteStableListsTx(tx *sql.Tx, callsiteStableID string, callsiteNodeID int64, rewrites map[string]string, dropped map[string]bool) error {
	if len(rewrites) == 0 && len(dropped) == 0 {
		return nil
	}
	rewrite := make(map[string]string, len(rewrites)+len(dropped))
	for old, next := range rewrites {
		rewrite[old] = next
	}
	for old := range dropped {
		rewrite[old] = ""
	}
	var coverage string
	err := tx.QueryRow(`SELECT COALESCE(pass_coverage,'[]') FROM edges
		WHERE type='HAS_CALLSITE' AND callsite_stable_id=?`, callsiteStableID).Scan(&coverage)
	if err != nil && err != sql.ErrNoRows {
		return fmt.Errorf("read pass coverage for %s: %w", callsiteStableID, err)
	}
	if err == nil && coverage != "" && coverage != "[]" {
		var passes []ResolutionPassCoverage
		if err := json.Unmarshal([]byte(coverage), &passes); err != nil {
			return fmt.Errorf("decode pass coverage for %s: %w", callsiteStableID, err)
		}
		for i := range passes {
			for _, field := range []*[]string{
				&passes[i].CandidateStableIDs, &passes[i].FlowTypeStableIDs,
				&passes[i].AllocationTypeStableIDs, &passes[i].ReachableStableIDs,
				&passes[i].RootStableIDs,
			} {
				out := make([]string, 0, len(*field))
				for _, id := range *field {
					if next, ok := rewrite[id]; ok {
						if next != "" {
							out = append(out, next)
						}
						continue
					}
					out = append(out, id)
				}
				*field = out
			}
		}
		if _, err := tx.Exec(`UPDATE edges SET pass_coverage=? WHERE type='HAS_CALLSITE' AND callsite_stable_id=?`,
			mustJSON(passes), callsiteStableID); err != nil {
			return fmt.Errorf("rewrite pass coverage for %s: %w", callsiteStableID, err)
		}
	}
	factRows, err := tx.Query(`SELECT id, COALESCE(candidate_stable_ids,'[]'), COALESCE(flow_type_stable_ids,'[]'),
		COALESCE(allocation_type_stable_ids,'[]'), COALESCE(reachable_stable_ids,'[]'), COALESCE(root_stable_ids,'[]')
		FROM nodes WHERE callsite_id=? AND node_type='completeness_fact'`, callsiteStableID)
	if err != nil {
		return fmt.Errorf("list completeness facts for %s: %w", callsiteStableID, err)
	}
	type factRow struct {
		id   int64
		cols [5]string
	}
	var facts []factRow
	for factRows.Next() {
		var r factRow
		if err := factRows.Scan(&r.id, &r.cols[0], &r.cols[1], &r.cols[2], &r.cols[3], &r.cols[4]); err != nil {
			factRows.Close()
			return fmt.Errorf("scan completeness fact: %w", err)
		}
		facts = append(facts, r)
	}
	if err := factRows.Close(); err != nil {
		return err
	}
	for _, r := range facts {
		rewritten := [5]string{}
		for i, raw := range r.cols {
			next, err := rewriteStableIDListJSON(raw, rewrite)
			if err != nil {
				return fmt.Errorf("rewrite fact %d id list: %w", r.id, err)
			}
			rewritten[i] = next
		}
		if _, err := tx.Exec(`UPDATE nodes SET candidate_stable_ids=?, flow_type_stable_ids=?,
			allocation_type_stable_ids=?, reachable_stable_ids=?, root_stable_ids=? WHERE id=?`,
			rewritten[0], rewritten[1], rewritten[2], rewritten[3], rewritten[4], r.id); err != nil {
			return fmt.Errorf("update completeness fact %d: %w", r.id, err)
		}
	}
	return nil
}

// attachUnresolvedFactTx publishes an UnresolvedFact node on a surviving
// callsite whose resolution the incremental reparse invalidated, linked by
// HAS_UNRESOLVED_FACT — the same shape insertResolutionV2FactsTx publishes on
// the full-build path. Idempotent on the fact's stable id so a repeat reparse
// records the state once.
func attachUnresolvedFactTx(tx *sql.Tx, callsiteStableID string, callsiteNodeID int64, passKind, reason string, remaining int, identity GraphCompletionIdentity) error {
	var filePath, language string
	if err := tx.QueryRow(`SELECT file_path, COALESCE(language,'') FROM nodes WHERE id=?`, callsiteNodeID).
		Scan(&filePath, &language); err != nil {
		return fmt.Errorf("read callsite %s location: %w", callsiteStableID, err)
	}
	factID := canonicalResolutionID(callsiteStableID, reason, strconv.Itoa(remaining), "incremental", identity.BuildID, identity.RepositoryRevision)
	var factNodeID int64
	err := tx.QueryRow(`SELECT id FROM nodes WHERE stable_id=?`, factID).Scan(&factNodeID)
	if err == sql.ErrNoRows {
		res, err := tx.Exec(`INSERT INTO nodes
			(label,name,qualified_name,file_path,language,stable_id,node_type,schema_version,source_revision,producer_build_id,
			 callsite_id,reason_code,candidate_count_v2,blocking_pass_kinds)
			VALUES ('UnresolvedFact',?,?,?,?,?,'unresolved_fact',2,?,?,?,?,?,?)`,
			reason, factID, filePath, language, factID, identity.RepositoryRevision, identity.BuildID,
			callsiteStableID, reason, remaining, mustJSON([]string{passKind}))
		if err != nil {
			return fmt.Errorf("insert unresolved fact for %s: %w", callsiteStableID, err)
		}
		factNodeID, err = res.LastInsertId()
		if err != nil {
			return err
		}
	} else if err != nil {
		return fmt.Errorf("lookup unresolved fact for %s: %w", callsiteStableID, err)
	}
	edgeID := canonicalResolutionID(callsiteStableID, "HAS_UNRESOLVED_FACT", factID)
	if _, err := tx.Exec(`INSERT OR IGNORE INTO edges
		(source_id,target_id,type,source_line,source_file,confidence,trust_tier,evidence_type,verification_status,stable_id,schema_version,callsite_stable_id)
		VALUES (?,?, 'HAS_UNRESOLVED_FACT',0,'',NULL,'STRUCTURAL','resolution_fact','structural_only',?,2,?)`,
		callsiteNodeID, factNodeID, edgeID, callsiteStableID); err != nil {
		return fmt.Errorf("link unresolved fact for %s: %w", callsiteStableID, err)
	}
	return nil
}

// recordUnresolvedIncomingTx records a dropped cross-file incoming edge (a
// legacy CALLS/IMPORTS row whose target symbol vanished from the reparsed
// file) as an UnresolvedFact hanging off the surviving SOURCE symbol. The
// fact's callsite_id slot carries a deterministic id derived from the dropped
// edge rather than a real callsite stable id — a legacy edge has no callsite
// row, and the synthetic anchor keeps the record honest (the fact names the
// exact edge that could not be re-pointed) without colliding with the
// callsite-keyed unresolved facts the v2 overlay publishes. Idempotent on the
// derived fact id, so a repeated reparse records the loss once.
func recordUnresolvedIncomingTx(tx *sql.Tx, r IncomingEdgeRef, filePath string, identity GraphCompletionIdentity) error {
	anchor := canonicalResolutionID(
		"incremental_incoming_edge", r.SourceFile, strconv.Itoa(r.SourceLine),
		r.EdgeType, r.TargetName, filePath)
	passKind := v2PassKind(r.ResolutionMethod)
	if _, ok := derivationPassKindsV2[passKind]; !ok {
		passKind = "legacy_unknown"
	}
	return attachUnresolvedFactTx(tx, anchor, r.SourceID, passKind, "no_viable_target", 0, identity)
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
		 signature, return_type, is_exported, is_test, language, parent_id,
		 file_hash, byte_start, byte_end)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		return nil, fmt.Errorf("prepare insert nodes: %w", err)
	}
	defer stmt.Close()

	ids := make([]int64, len(nodes))
	for i, n := range nodes {
		fileHash, byteStart, byteEnd := contentAddress(n)
		res, err := stmt.Exec(
			n.Label, n.Name, n.QualifiedName, n.FilePath, n.StartLine, n.EndLine,
			n.Signature, n.ReturnType, n.IsExported, n.IsTest, n.Language, nullableParentID(n.ParentID),
			fileHash, byteStart, byteEnd,
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
		 resolution_method, confidence, metadata, trust_tier, candidate_count, evidence_type, verification_status, access_sites, actual_args)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
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
			nullableText(e.AccessSites), nullableText(e.ActualArgs),
		); err != nil {
			return fmt.Errorf("insert edge %d: %w", i, err)
		}
	}
	return nil
}

func BatchInsertResolutionSymbolsTx(tx *sql.Tx, symbols []*ResolutionSymbol) error {
	if len(symbols) == 0 {
		return nil
	}
	stmt, err := tx.Prepare(`INSERT OR REPLACE INTO resolution_symbols
		(stable_id,native_id,native_kind,normalized_kind,language,path,qualified_name,start_line,end_line,export_status)
		VALUES (?,?,?,?,?,?,?,?,?,?)`)
	if err != nil {
		return fmt.Errorf("prepare resolution symbols: %w", err)
	}
	defer stmt.Close()
	for _, s := range symbols {
		if _, err := stmt.Exec(s.StableID, s.NativeID, s.NativeKind, s.NormalizedKind, s.Language, s.Path, s.QualifiedName, s.StartLine, s.EndLine, s.ExportStatus); err != nil {
			return fmt.Errorf("insert resolution symbol: %w", err)
		}
	}
	return nil
}

func BatchInsertResolutionCallsitesTx(tx *sql.Tx, callsites []*ResolutionCallsite) error {
	if len(callsites) == 0 {
		return nil
	}
	stmt, err := tx.Prepare(`INSERT OR REPLACE INTO resolution_callsites
		(callsite_id,callsite_ordinal,repository_revision,source_stable_id,source_native_id,source_id,source_line,source_file,callee,language,dispatch_state,candidate_count,selected_target_stable_id,selected_target_native_id,mechanism,verification_status)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`)
	if err != nil {
		return fmt.Errorf("prepare resolution callsites: %w", err)
	}
	defer stmt.Close()
	for _, c := range callsites {
		if _, err := stmt.Exec(c.CallsiteID, c.CallsiteOrdinal, c.RepositoryRevision, c.SourceStableID, c.SourceNativeID, c.SourceID, c.SourceLine, c.SourceFile, c.Callee, c.Language, c.DispatchState, c.CandidateCount, c.SelectedTargetStableID, c.SelectedTargetNativeID, c.Mechanism, c.VerificationStatus); err != nil {
			return fmt.Errorf("insert resolution callsite: %w", err)
		}
	}
	return nil
}

func BatchInsertResolutionCandidatesTx(tx *sql.Tx, candidates []*ResolutionCandidate) error {
	if len(candidates) == 0 {
		return nil
	}
	return fmt.Errorf("legacy resolution_candidates publication disabled; use canonical CANDIDATE_TARGET edges")
}

// BatchInsertPropertiesTx inserts properties inside the given tx.
func BatchInsertPropertiesTx(tx *sql.Tx, props []*Property) error {
	if len(props) > 0 {
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
	}
	// Maintained in the caller's transaction, and unconditionally — an empty
	// batch is a reindexed file that lost all its facts, and its rows have
	// already been DELETEd above by DeleteFileEdgesAndNodesTx. Skipping it
	// there is exactly how an external-content index outlives the content it
	// points at. See internal/store/properties_fts.go.
	if err := PopulatePropertiesFTS5Tx(tx); err != nil {
		return fmt.Errorf("index properties: %w", err)
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
	_, err := tx.Exec(`UPDATE nodes SET parent_id = ? WHERE id = ?`, nullableParentID(parentID), nodeID)
	return err
}
