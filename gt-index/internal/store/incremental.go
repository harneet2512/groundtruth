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
	"fmt"
	"os"
	"strings"
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
	// TargetSignature is the target node's signature — the R#2 identity witness.
	// When qualified_name does NOT uniquely re-prove identity (idiomatic Go
	// cross-file-receiver methods — `types.go` declares the type, `methods.go`
	// defines the method — carry the BARE name as qualified_name, so ≥2 same-named
	// such methods collide on qname), a UNIQUE signature match among the same-named
	// candidates re-proves WHICH node the edge meant. The signature embeds the
	// receiver for Go (`func (b *Beta) Reset()`) and the full parameter list for
	// every language, so same-named twins are distinguishable by it — the restore
	// preserves the type-aware tier onto the RIGHT target instead of stripping to
	// name_match@ids[0] (LIPI #1 residual: L6 making the map WORSE post-edit).
	TargetSignature string
	// Metadata is the edge's metadata column (B6) — e.g. api_edges route info. It
	// is source-side (call-site) data unchanged by re-parsing the TARGET file, so a
	// -file reindex must RESTORE it verbatim rather than nulling it (info-loss).
	Metadata string
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
func SnapshotIncomingEdgesTx(tx *sql.Tx, filePath string, cap int) ([]IncomingEdgeRef, error) {
	if cap <= 0 {
		cap = 50000
	}
	rows, err := tx.Query(
		`SELECT e.source_id, e.source_line, e.type, COALESCE(e.source_file, ''), n.name,
		        COALESCE(e.resolution_method, ''), COALESCE(e.confidence, 0.0),
		        COALESCE(e.evidence_type, ''), COALESCE(n.qualified_name, ''),
		        COALESCE(e.metadata, ''), COALESCE(n.signature, '')
		   FROM edges e
		   JOIN nodes n ON e.target_id = n.id
		  WHERE n.file_path = ?
		    AND (e.source_file IS NULL OR e.source_file != ?)
		    AND e.source_id NOT IN (SELECT id FROM nodes WHERE file_path = ?)
		    AND (e.resolution_method IS NULL OR e.resolution_method NOT LIKE 'promote_%')
		  ORDER BY e.id
		  LIMIT ?`,
		filePath, filePath, filePath, cap,
	)
	// B-9: ORDER BY e.id makes the cap boundary DETERMINISTIC — when the LIMIT is hit
	// (B7), *which* incoming edges survive is now id-ordered, not SQLite-scan-order
	// dependent (parity with the determinism the resolver/closure already enforce).
	if err != nil {
		return nil, fmt.Errorf("snapshot incoming edges for %s: %w", filePath, err)
	}
	defer rows.Close()

	var out []IncomingEdgeRef
	for rows.Next() {
		var r IncomingEdgeRef
		if err := rows.Scan(&r.SourceID, &r.SourceLine, &r.EdgeType, &r.SourceFile, &r.TargetName,
			&r.ResolutionMethod, &r.Confidence, &r.EvidenceType, &r.TargetQualifiedName, &r.Metadata,
			&r.TargetSignature); err != nil {
			return nil, fmt.Errorf("scan incoming edge: %w", err)
		}
		out = append(out, r)
	}
	// B7: the LIMIT is a defensive bound, but a SILENT truncation would delete
	// incoming edges (DeleteFileEdgesAndNodesTx) that this snapshot then fails to
	// restore → permanent edge loss until the source files are themselves reindexed.
	// Never silent (CLAUDE.md "no silent caps"): flag it loudly when the cap is hit so
	// a hub over the bound is visible; a full reindex recovers. len(out)==cap is the
	// only observable signal short of a separate COUNT(*).
	if len(out) == cap {
		fmt.Fprintf(os.Stderr, "WARNING: SnapshotIncomingEdgesTx hit the %d-row cap for %s — "+
			"incoming cross-file edges beyond the cap will NOT be restored after this reindex; "+
			"run a full index if edge loss is suspected\n", cap, filePath)
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

// typeOrUniquenessDerivedMethods are the deterministic methods whose tier was earned
// by a fact a BARE NAME does not re-prove: a receiver TYPE (type_flow/import_type/
// inherited/impl_method/unique_method/return_type), GLOBAL UNIQUENESS at index time
// (verified_unique), or a language-server go-to-definition proof (lsp/lsp_verified).
// When an incremental restore can only re-match by bare name (no surviving node whose
// qualified_name equals the original target's), the original receiver-type / uniqueness /
// LSP-definition context is GONE — re-stamping the original CERTIFIED tier onto whatever
// single same-named node now occupies the file would launder the tier onto a possibly-
// wrong target. These restore CAPPED at CANDIDATE (0.6). The `-file`/L6 incremental path
// never re-runs the LSP, so an `lsp`/`lsp_verified` edge cannot re-prove its go-to-
// definition target here — it belongs in the cap set exactly like the type-derived rungs.
// The signature-derived `same_file`/`import` methods are NOT here: their proof (the call
// is in the same file / the file imports the name) survives a bare-name re-match.
var typeOrUniquenessDerivedMethods = map[string]bool{
	"verified_unique": true,
	"type_flow":       true,
	"import_type":     true,
	"inherited":       true,
	"impl_method":     true,
	"unique_method":   true,
	"return_type":     true,
	"lsp":             true,
	"lsp_verified":    true,
}

// uniquenessDerivedMethods are the subset whose tier premise is GLOBAL NAME-UNIQUENESS at
// index time (verified_unique = only one function of this name; unique_method = only one
// method of this name). An exact qualified_name re-match re-proves TARGET IDENTITY but NOT
// uniqueness — so if the reindexed file now holds MORE THAN ONE same-named node (len(ids)>1),
// the uniqueness premise is observably falsified and the CERTIFIED tier must not be preserved
// (it would ship candidate_count>1 alongside verified_unique — a self-contradictory fact on
// the fact surface). Type/LSP-derived tiers (type_flow/import_type/inherited/impl_method/
// return_type/lsp/lsp_verified) are NOT here: the qname re-match re-proves their receiver-type
// / go-to-definition identity even amid same-named siblings.
var uniquenessDerivedMethods = map[string]bool{
	"verified_unique": true,
	"unique_method":   true,
}

// stripReceiverTypeTag removes ONLY the `receiver_type=<...>` segment(s) from a
// `;`-separated metadata string, preserving every other key (dataflow=, api_edges
// route info, …) verbatim — one segment is removed, never the field.
//
// W-B invariant (producer cleanliness): `receiver_type=<T>` on edges.metadata is
// CALL-SITE PROVENANCE minted by the resolver's receiver-PROVEN rungs — non-empty
// IFF the receiver type was structurally proven. When an incremental restore
// CANNOT re-prove target identity (!identityReproven), the receiver-type proof is
// exactly what the bare-name re-match failed to re-prove: the restored edge demotes
// (0.6 cap / name_match fallback) and MUST NOT keep a fact-shaped receiver tag.
// The identity-REPROVEN path restores provenance verbatim, exactly like every
// other metadata key (the B6 contract).
func stripReceiverTypeTag(meta string) string {
	if meta == "" || !strings.Contains(meta, "receiver_type=") {
		return meta
	}
	parts := strings.Split(meta, ";")
	kept := parts[:0]
	for _, p := range parts {
		if strings.HasPrefix(p, "receiver_type=") {
			continue
		}
		kept = append(kept, p)
	}
	return strings.Join(kept, ";")
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
	// explicitly deterministic, not an accident of SQLite scan order. Also select
	// qualified_name so the restore can re-prove TARGET IDENTITY against the original
	// edge's TargetQualifiedName (P0: a bare-name re-match must not launder a verified
	// tier onto a different node that happens to share the simple name).
	lookup, err := tx.Prepare(`SELECT id, COALESCE(qualified_name, ''), COALESCE(signature, '') FROM nodes WHERE name = ? AND file_path = ? ORDER BY id`)
	if err != nil {
		return 0, 0, fmt.Errorf("prepare incoming lookup: %w", err)
	}
	defer lookup.Close()
	ins, err := tx.Prepare(
		`INSERT INTO edges (source_id, target_id, type, source_line, source_file,
		 resolution_method, confidence, metadata, trust_tier, candidate_count, evidence_type, verification_status)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unverified')`,
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
		// qnameMatchCount counts how many candidates carry that qualified_name:
		// identity is re-proven ONLY when it is UNIQUE (==1). Several nodes sharing
		// a qualified_name (Python @overload stacks / platform-conditional defs —
		// qualified_name is bare `name` for top-level funcs, so it is NOT unique in
		// a file) do NOT re-prove WHICH node the edge meant; preserving a CERTIFIED
		// tier onto the arbitrary first would launder it (Fable 2026-07-03).
		var qnameMatchID, sigMatchID int64
		var qnameMatchCount, sigMatchCount int
		for rows.Next() {
			var id int64
			var qname, sig string
			if err := rows.Scan(&id, &qname, &sig); err != nil {
				rows.Close()
				return restored, unresolved, fmt.Errorf("scan target id: %w", err)
			}
			ids = append(ids, id)
			if r.TargetQualifiedName != "" && qname == r.TargetQualifiedName {
				qnameMatchCount++
				if qnameMatchID == 0 {
					qnameMatchID = id
				}
			}
			// R#2 signature witness: a UNIQUE non-empty signature match re-proves
			// identity when qualified_name cannot (bare-name collision). sigMatchCount>1
			// (identical signatures — a genuine @overload) is ambiguous → not a witness;
			// the edge falls through to the name_match guess (correct-or-quiet).
			if r.TargetSignature != "" && sig == r.TargetSignature {
				sigMatchCount++
				if sigMatchID == 0 {
					sigMatchID = id
				}
			}
		}
		rows.Close()

		if len(ids) == 0 {
			unresolved++
			continue
		}

		// Target node for the restored edge: the exact-qualified-name match when one
		// exists (identity re-proven), else a UNIQUE signature match (R#2), else the
		// deterministic first candidate (id ASC). identityReproven gates whether a
		// type/uniqueness-derived tier may be PRESERVED: without a re-match on qname OR
		// signature the bare name does not re-prove the original receiver-type /
		// global-uniqueness fact, so that tier is capped at CANDIDATE.
		targetID := ids[0]
		qnameMatched := false
		if qnameMatchID != 0 && qnameMatchCount == 1 {
			targetID = qnameMatchID
			qnameMatched = true
		}
		// R#2 signature witness — the LIPI #1 residual fix. When qualified_name did
		// NOT uniquely re-prove identity (idiomatic Go cross-file-receiver twins share
		// the BARE `name` as qualified_name, so qnameMatchCount>1), a UNIQUE non-empty
		// signature match disambiguates WHICH same-named node the edge meant: the
		// signature embeds the receiver (`func (b *Beta) Reset()`) and the full param
		// list, so twins are distinguishable by it for every language. An @overload /
		// platform-conditional stack whose signatures are IDENTICAL stays ambiguous
		// (sigMatchCount>1) → falls through to name_match (correct-or-quiet). Without
		// this, editing a file with two same-named cross-file-receiver methods strips
		// an incoming type_flow/impl_method/inherited/lsp edge to name_match@ids[0] —
		// L6 making the map WORSE post-edit (the verified caller vanishes).
		sigMatched := false
		if !qnameMatched && r.TargetSignature != "" && sigMatchID != 0 && sigMatchCount == 1 {
			targetID = sigMatchID
			sigMatched = true
		}
		identityReproven := qnameMatched || sigMatched

		// PARITY with the full-index resolver's qualifiedUnresolved gate
		// (resolver.go:721,743-747): if the ORIGINAL edge was a qualified
		// stdlib-shadow the resolver already demoted, the incremental restore
		// must NOT re-launder it back to CERTIFIED. The only stored signal of
		// that demotion is the edge's evidence_type marker; an `import`/
		// `same_file` row that still carries it is a laundered legacy edge and
		// is treated as demoted too (correct-or-quiet — never re-promote a guess).
		qualifiedUnresolved := r.EvidenceType == "name_match_qualified_unresolved"

		// #B6/R1: preserve the original deterministic method + confidence verbatim
		// (re-deriving the tier from the ONE threshold table) when identity is
		// re-proven — EITHER a single surviving candidate (unambiguous) OR an exact
		// qualified_name re-match among several same-named nodes (qnameMatched, which
		// already pointed targetID at the RIGHT node above). Gating on len(ids)==1
		// alone was STRICTER than the documented design (lines 248-250: qnameMatched is
		// the type-tier gate) — it silently downgraded an lsp/type_flow/verified_unique
		// edge to a 0.2-0.6 name_match guess the moment the reindexed file gained a
		// SECOND same-named method, so a verified caller vanished from the contract
		// pillar post-edit even though its exact target was re-found. The multi-candidate
		// case WITHOUT a qname OR signature re-match still falls to the name_match guess
		// (genuinely ambiguous), and the P0 demote guard below still caps type/uniqueness/
		// LSP tiers whenever !identityReproven — so a bare-name re-match never re-certifies
		// a guess (R#2 adds the signature witness to the identity re-proof — see below).
		var conf float64
		var method string
		var tier string
		var evType string
		// W-B provenance strip: TRUE on every restore branch whose demote means the
		// receiver-type proof did NOT survive the reindex (the !identityReproven cap
		// and the whole name_match/qualifiedUnresolved fallback). The restored
		// metadata then loses ONLY its `receiver_type=` segment — a demoted/guess
		// edge must not carry a fact-shaped receiver tag (invariant: non-empty IFF
		// receiver-PROVEN). All other keys restore verbatim (B6).
		stripProvenance := false
		if !qualifiedUnresolved && (len(ids) == 1 || identityReproven) && deterministicRestoreMethods[r.ResolutionMethod] {
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
			// P0 DEMOTE: a type/uniqueness/LSP-derived tier (verified_unique/type_flow/
			// import_type/inherited/impl_method/unique_method/return_type/lsp/lsp_verified)
			// was earned by a receiver TYPE, GLOBAL UNIQUENESS, or an LSP go-to-definition
			// proof that a bare-name re-match does NOT re-prove (the -file/L6 path never
			// re-runs the LSP). If we could not re-prove target identity via an exact
			// qualified_name match, the single surviving same-named node may be a
			// DIFFERENT symbol (rename-and-replace) — preserving the CERTIFIED tier
			// would launder it onto the wrong target. Cap at CANDIDATE (0.6) and let
			// the tier re-derive to CANDIDATE. The signature-derived same_file/import
			// methods are exempt: their proof survives a bare-name re-match.
			if !identityReproven && typeOrUniquenessDerivedMethods[method] {
				if conf > 0.6 {
					conf = 0.6
				}
				// The receiver-type / uniqueness / LSP proof was NOT re-proven by the
				// bare-name re-match — the capped edge must not keep receiver provenance.
				stripProvenance = true
			}
			// Fable finding 1: even WITH an exact qname re-match, a UNIQUENESS-derived tier
			// (verified_unique/unique_method) cannot be preserved when the reindexed file now
			// holds >1 same-named node — the qname re-proves identity but the uniqueness premise
			// is falsified (candidate_count>1). Preserving CERTIFIED here launders a
			// self-contradictory fact onto the -file/L6 fact surface (the P0 class). Cap at
			// CANDIDATE (0.6); type/LSP tiers are exempt (qname re-proves their identity).
			// NOTE: the tier here is capped by CONFIDENCE (0.6 → CANDIDATE) and the method is
			// deliberately PRESERVED as provenance (TestResolveIncomingEdgesPreservesLSPEdge).
			// The fact-gate that reads this MUST require conf ≥ EDGE_CONFIDENCE_FLOOR alongside
			// the method whitelist — see v1r_brief `is_fact` (the conf conjunct closes the
			// method-only launder for these capped-but-whitelisted restores).
			if identityReproven && len(ids) > 1 && uniquenessDerivedMethods[method] && conf > 0.6 {
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
			// A name_match restore is a GUESS — it must never carry a fact-shaped
			// receiver tag, whatever the original edge earned (correct-or-quiet).
			stripProvenance = true
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
		// B6: restore metadata verbatim (NULL when the original was empty, matching
		// prior behavior for metadata-less edges — so only real metadata is carried).
		// W-B: on a demoted/not-reproven restore, the `receiver_type=` provenance
		// segment is stripped first (other keys untouched — see stripReceiverTypeTag).
		restoredMeta := r.Metadata
		if stripProvenance {
			restoredMeta = stripReceiverTypeTag(restoredMeta)
		}
		var edgeMeta interface{}
		if restoredMeta != "" {
			edgeMeta = restoredMeta
		}
		if _, err := ins.Exec(r.SourceID, targetID, r.EdgeType, r.SourceLine, srcFile,
			method, conf, edgeMeta, tier, len(ids), evType); err != nil {
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
	// LIPI (reindex orphan-proofing): delete the source side SYMMETRICALLY — by the
	// denormalized source_file string AND by source_id ∈ (nodes of this file). The
	// string alone left an orphan when source_file was NULL/≠relSlash for an edge
	// whose source node lives in this file (its node gets deleted below, the edge
	// survives → source_id → dead id). Adding the source_id subquery makes the delete
	// structurally orphan-proof; target_id already covered the incoming side.
	resE, err := tx.Exec(
		`DELETE FROM edges
		   WHERE source_file = ?
		      OR source_id IN (SELECT id FROM nodes WHERE file_path = ?)
		      OR target_id IN (SELECT id FROM nodes WHERE file_path = ?)`,
		filePath, filePath, filePath,
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
			n.Signature, n.ReturnType, n.IsExported, n.IsTest, n.Language, nullableParentID(n.ParentID),
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
		`INSERT INTO properties (node_id, kind, value, line, confidence,
		   property_id, start_line, end_line, extractor, evidence_method,
		   trust_tier, verification_status, source_revision)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		return fmt.Errorf("prepare insert properties: %w", err)
	}
	defer stmt.Close()
	for i, p := range props {
		p.fillProvenance()
		if _, err := stmt.Exec(p.NodeID, p.Kind, p.Value, p.Line, p.Confidence,
			p.PropertyID, p.StartLine, p.EndLine, p.Extractor, p.EvidenceMethod,
			p.TrustTier, p.VerificationStatus, p.SourceRevision); err != nil {
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
	_, err := tx.Exec(`UPDATE nodes SET parent_id = ? WHERE id = ?`, nullableParentID(parentID), nodeID)
	return err
}
