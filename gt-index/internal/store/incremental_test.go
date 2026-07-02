package store

import (
	"database/sql"
	"path/filepath"
	"strings"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

func TestResolveIncomingEdgesDoesNotCertifyNameMatch(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := createSchema(db); err != nil {
		t.Fatal(err)
	}

	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()

	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, file_path, language) VALUES
		 (1, 'Function', 'caller', 'src/caller.py', 'python'),
		 (2, 'Function', 'target', 'src/target.py', 'python')`,
	); err != nil {
		t.Fatal(err)
	}

	snap := []IncomingEdgeRef{{
		SourceID:         1,
		SourceLine:       12,
		EdgeType:         "CALLS",
		SourceFile:       "src/caller.py",
		TargetName:       "target",
		ResolutionMethod: "name_match",
		Confidence:       0.9,
	}}
	restored, unresolved, err := ResolveIncomingEdgesTx(tx, snap, "src/target.py")
	if err != nil {
		t.Fatal(err)
	}
	if restored != 1 || unresolved != 0 {
		t.Fatalf("restored=%d unresolved=%d", restored, unresolved)
	}

	var method, tier, evidenceType, verificationStatus string
	var confidence float64
	if err := tx.QueryRow(
		`SELECT resolution_method, trust_tier, evidence_type, verification_status, confidence
		   FROM edges WHERE source_id = 1 AND target_id = 2`,
	).Scan(&method, &tier, &evidenceType, &verificationStatus, &confidence); err != nil {
		t.Fatal(err)
	}
	if method != "name_match" {
		t.Fatalf("method=%q", method)
	}
	if tier == "CERTIFIED" {
		t.Fatalf("name_match restored as CERTIFIED")
	}
	if evidenceType != "name_match" || verificationStatus != "unverified" {
		t.Fatalf("evidence_type=%q verification_status=%q", evidenceType, verificationStatus)
	}
	// #B6 split-brain fix: the restore used to store conf 0.9 with tier
	// SPECULATIVE — tierFor(0.9) is CERTIFIED, so conf and tier disagreed. A
	// single-candidate name_match re-match now restores at 0.6 (the ambiguity
	// score) so tier == tierFor(conf) AND name_match stays below CERTIFIED.
	if confidence >= 0.9 {
		t.Fatalf("confidence=%v; cc==1 name_match restore must stay below the CERTIFIED threshold", confidence)
	}
	if tier != tierForConfidence(confidence) {
		t.Fatalf("tier %q does not follow confidence %v (tierFor says %q)", tier, confidence, tierForConfidence(confidence))
	}
}

// Item #3 (LIPI finding #1): a qualified stdlib-shadow caller the FULL-index
// resolver already demoted (evidence_type = name_match_qualified_unresolved)
// must NOT be re-laundered to CERTIFIED on the incremental (`-file`) restore
// path. Before the fix, the snapshot dropped evidence_type and the restore
// re-stamped the single import/same_file candidate CERTIFIED conf=1.0 — exactly
// the P0 stdlib-shadow laundering, reopened on the incremental path.
func TestResolveIncomingEdgesDoesNotRelaunderQualifiedUnresolved(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := createSchema(db); err != nil {
		t.Fatal(err)
	}

	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()

	// caller's call site was `os.walk(...)`; the project has a same-named `walk`
	// in walk.py. The resolver demoted that to name_match_qualified_unresolved.
	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, qualified_name, file_path, language) VALUES
		 (1, 'Function', 'caller', 'caller.walk_caller', 'src/caller.py', 'python'),
		 (2, 'Function', 'walk',   'account.walk',       'src/walk.py',   'python')`,
	); err != nil {
		t.Fatal(err)
	}

	// The ORIGINAL edge carries the demotion marker. (resolution_method is
	// import here to model the legacy-launder vector the report calls out: even
	// an import/same_file row that still carries the marker must stay demoted.)
	snap := []IncomingEdgeRef{{
		SourceID:            1,
		SourceLine:          12,
		EdgeType:            "CALLS",
		SourceFile:          "src/caller.py",
		TargetName:          "walk",
		ResolutionMethod:    "import",
		Confidence:          0.9,
		EvidenceType:        "name_match_qualified_unresolved",
		TargetQualifiedName: "account.walk",
	}}
	restored, unresolved, err := ResolveIncomingEdgesTx(tx, snap, "src/walk.py")
	if err != nil {
		t.Fatal(err)
	}
	if restored != 1 || unresolved != 0 {
		t.Fatalf("restored=%d unresolved=%d", restored, unresolved)
	}

	var method, tier, evidenceType string
	var confidence float64
	if err := tx.QueryRow(
		`SELECT resolution_method, trust_tier, evidence_type, confidence
		   FROM edges WHERE source_id = 1 AND target_id = 2`,
	).Scan(&method, &tier, &evidenceType, &confidence); err != nil {
		t.Fatal(err)
	}
	if tier == "CERTIFIED" {
		t.Fatalf("qualified-unresolved stdlib-shadow re-laundered as CERTIFIED on incremental restore")
	}
	if method != "name_match" {
		t.Fatalf("method=%q; demoted edge must restore as name_match, not import", method)
	}
	if evidenceType != "name_match_qualified_unresolved" {
		t.Fatalf("evidence_type=%q; demotion marker must be preserved across reindex", evidenceType)
	}
	// #B6: parity with the resolver demote — a demoted stdlib-shadow restores at
	// the demoted confidence (0.2/SPECULATIVE), it must not climb back to 0.9
	// via the single-candidate name_match row.
	if confidence > 0.2 {
		t.Fatalf("confidence=%v; demoted edge must restore at the demoted confidence (0.2)", confidence)
	}
	if tier != "SPECULATIVE" {
		t.Fatalf("tier=%q; demoted edge must restore SPECULATIVE", tier)
	}
}

// #B6: an lsp edge must be PRESERVED across an incremental `-file` reindex — its
// method ("lsp") and evidence marker ("lsp_definition") survive, never stripped
// down to a name_match guess. Before the #B6 fix the preserve condition was
// {same_file, import} only, so every lsp/type_flow/inherited/verified_unique edge
// targeting a reindexed file was stripped to a name_match guess.
// P0 refinement (this fixture sets NO qualified_name → qnameMatched=false): an LSP
// go-to-definition proof is earned by a discriminator finer than name+file (which
// same-named method the call binds to), and the -file/L6 path never re-runs the
// LSP — so a bare name+file re-match cannot re-establish it. The tier is therefore
// CAPPED at CANDIDATE (0.6); method + evidence marker are still preserved (not a
// strip-to-guess). The companion TestResolveIncomingEdgesPreservesLSPEdgeOnQNameMatch
// pins that an exact-qname re-match preserves the full 0.95/CERTIFIED tier — the cap
// is targeted at identity-unproven restores, not a blanket lsp teardown.
func TestResolveIncomingEdgesPreservesLSPEdge(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := createSchema(db); err != nil {
		t.Fatal(err)
	}

	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()

	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, file_path, language) VALUES
		 (1, 'Function', 'caller', 'src/caller.py', 'python'),
		 (2, 'Method',   'save',   'src/models.py', 'python')`,
	); err != nil {
		t.Fatal(err)
	}

	snap := []IncomingEdgeRef{{
		SourceID:         1,
		SourceLine:       12,
		EdgeType:         "CALLS",
		SourceFile:       "src/caller.py",
		TargetName:       "save",
		ResolutionMethod: "lsp",
		Confidence:       0.95,
		EvidenceType:     "lsp_definition",
	}}
	restored, unresolved, err := ResolveIncomingEdgesTx(tx, snap, "src/models.py")
	if err != nil {
		t.Fatal(err)
	}
	if restored != 1 || unresolved != 0 {
		t.Fatalf("restored=%d unresolved=%d", restored, unresolved)
	}

	var method, tier, evidenceType string
	var confidence float64
	if err := tx.QueryRow(
		`SELECT resolution_method, trust_tier, evidence_type, confidence
		   FROM edges WHERE source_id = 1 AND target_id = 2`,
	).Scan(&method, &tier, &evidenceType, &confidence); err != nil {
		t.Fatal(err)
	}
	if method != "lsp" {
		t.Fatalf("method=%q; lsp edge must restore as lsp, not be stripped to a guess", method)
	}
	if confidence != 0.6 {
		t.Fatalf("confidence=%v; an lsp restore without a qualified_name re-proof must cap at CANDIDATE 0.6 (the -file path never re-runs the LSP)", confidence)
	}
	if tier != "CANDIDATE" {
		t.Fatalf("tier=%q; a capped-0.6 lsp restore re-derives to CANDIDATE", tier)
	}
	if evidenceType != "lsp_definition" {
		t.Fatalf("evidence_type=%q; original evidence marker must be preserved", evidenceType)
	}
}

// P0 refinement companion to TestResolveIncomingEdgesPreservesLSPEdge: WITH an exact
// qualified_name re-match the lsp go-to-definition target identity IS re-proven, so
// the qname-cap does NOT fire and the full 0.95/CERTIFIED tier is preserved. Pins
// that the cap is targeted at identity-unproven restores (rename-and-replace defence)
// only — not a blanket lsp teardown — mirroring the verified_unique demote/preserve pair.
func TestResolveIncomingEdgesPreservesLSPEdgeOnQNameMatch(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := createSchema(db); err != nil {
		t.Fatal(err)
	}

	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()

	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, qualified_name, file_path, language) VALUES
		 (1, 'Function', 'caller', 'src.caller.caller',     'src/caller.py', 'python'),
		 (2, 'Method',   'save',   'src.models.Model.save', 'src/models.py', 'python')`,
	); err != nil {
		t.Fatal(err)
	}

	snap := []IncomingEdgeRef{{
		SourceID:            1,
		SourceLine:          12,
		EdgeType:            "CALLS",
		SourceFile:          "src/caller.py",
		TargetName:          "save",
		TargetQualifiedName: "src.models.Model.save",
		ResolutionMethod:    "lsp",
		Confidence:          0.95,
		EvidenceType:        "lsp_definition",
	}}
	restored, unresolved, err := ResolveIncomingEdgesTx(tx, snap, "src/models.py")
	if err != nil {
		t.Fatal(err)
	}
	if restored != 1 || unresolved != 0 {
		t.Fatalf("restored=%d unresolved=%d", restored, unresolved)
	}

	var method, tier, evidenceType string
	var confidence float64
	if err := tx.QueryRow(
		`SELECT resolution_method, trust_tier, evidence_type, confidence
		   FROM edges WHERE source_id = 1 AND target_id = 2`,
	).Scan(&method, &tier, &evidenceType, &confidence); err != nil {
		t.Fatal(err)
	}
	if method != "lsp" {
		t.Fatalf("method=%q; lsp edge must restore as lsp", method)
	}
	if confidence != 0.95 {
		t.Fatalf("confidence=%v; an exact-qname re-match re-proves identity and must preserve 0.95", confidence)
	}
	if tier != "CERTIFIED" {
		t.Fatalf("tier=%q; tierFor(0.95) is CERTIFIED when identity is re-proven", tier)
	}
	if evidenceType != "lsp_definition" {
		t.Fatalf("evidence_type=%q; original evidence marker must be preserved", evidenceType)
	}
}

// Item #4 (LIPI finding #2): the restore must floor ONLY the literal pre-v14
// 0.0/NULL sentinel to the verified value; any conf>0 the pipeline previously
// stored (incl. an intentionally-lowered one) is PRESERVED verbatim. Before the
// fix, `conf<0.5 -> 1.0` re-certified a deliberately-demoted caller on reindex.
func TestResolveIncomingEdgesPreservesIntentionallyLoweredConfidence(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := createSchema(db); err != nil {
		t.Fatal(err)
	}

	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()

	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, file_path, language) VALUES
		 (1, 'Function', 'callerA', 'src/a.py', 'python'),
		 (2, 'Function', 'targetA', 'src/t.py', 'python'),
		 (3, 'Function', 'callerB', 'src/b.py', 'python')`,
	); err != nil {
		t.Fatal(err)
	}

	snap := []IncomingEdgeRef{
		{ // intentionally-lowered import caller at 0.2 — must be PRESERVED at 0.2
			SourceID: 1, SourceLine: 5, EdgeType: "CALLS", SourceFile: "src/a.py",
			TargetName: "targetA", ResolutionMethod: "import", Confidence: 0.2,
		},
		{ // pre-v14 sentinel 0.0 — SHOULD be floored to the verified value 1.0
			SourceID: 3, SourceLine: 7, EdgeType: "CALLS", SourceFile: "src/b.py",
			TargetName: "targetA", ResolutionMethod: "import", Confidence: 0.0,
		},
	}
	restored, _, err := ResolveIncomingEdgesTx(tx, snap, "src/t.py")
	if err != nil {
		t.Fatal(err)
	}
	if restored != 2 {
		t.Fatalf("restored=%d", restored)
	}

	var lowered float64
	if err := tx.QueryRow(`SELECT confidence FROM edges WHERE source_id = 1 AND target_id = 2`).Scan(&lowered); err != nil {
		t.Fatal(err)
	}
	if lowered != 0.2 {
		t.Fatalf("intentionally-lowered confidence=%v; must be preserved at 0.2 (was re-certified)", lowered)
	}

	var sentinel float64
	if err := tx.QueryRow(`SELECT confidence FROM edges WHERE source_id = 3 AND target_id = 2`).Scan(&sentinel); err != nil {
		t.Fatal(err)
	}
	if sentinel != 1.0 {
		t.Fatalf("pre-v14 0.0 sentinel confidence=%v; must be floored to verified 1.0", sentinel)
	}
}

// Item #45 (LIPI finding #5): InsertAssertion must persist resolution_score,
// matching BatchInsertAssertions. Before the fix the single-row inserter
// omitted the column, silently storing the schema default 0.0.
func TestInsertAssertionPersistsResolutionScore(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	db, err := Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	if _, err := db.db.Exec(
		`INSERT INTO nodes (id, label, name, file_path, language, is_test) VALUES
		 (1, 'Function', 'test_it',  'tests/t_test.py', 'python', 1),
		 (2, 'Function', 'target_fn','src/target.py',   'python', 0)`,
	); err != nil {
		t.Fatal(err)
	}

	if err := db.InsertAssertion(&Assertion{
		TestNodeID:      1,
		TargetNodeID:    2,
		ResolutionScore: 0.875,
		Kind:            "assertEqual",
		Expression:      "assertEqual(target_fn(), 3)",
		Expected:        "3",
		Line:            10,
	}); err != nil {
		t.Fatal(err)
	}

	var score float64
	if err := db.db.QueryRow(
		`SELECT resolution_score FROM assertions WHERE test_node_id = 1 AND target_node_id = 2`,
	).Scan(&score); err != nil {
		t.Fatal(err)
	}
	if score != 0.875 {
		t.Fatalf("resolution_score=%v; single-row InsertAssertion dropped the score (expected 0.875)", score)
	}
}

// G02 (HIGH, plumbing): SnapshotIncomingEdgesTx must EXCLUDE incoming promoted
// depth edges (resolution_method LIKE 'promote_%'). The whole-graph promote pass
// (resolver.PromotePropertyEdges, Pass 4f) regenerates every promoted edge AFTER
// the incremental tx commits, and its idempotence DELETE matches only 'promote_%'.
// If an incoming promoted edge (e.g. another file's method READS a class in the
// reindexed file) were snapshotted, it would be deleted by the target_id-keyed
// DELETE, restored by ResolveIncomingEdgesTx as a phantom name_match-tier edge the
// promote DELETE can never reach, then duplicated when the promote pass re-emits the
// genuine promote_* edge. Excluding it from the snapshot is the convergence fix.
//
// Red before the fix: the unfiltered SELECT snapshots BOTH edges (len==2) and the
// READS edge restores as a name_match phantom. Green after: only the genuine CALLS
// edge is snapshotted (len==1); the promoted edge is left to the post-commit pass.
func TestSnapshotIncomingEdgesExcludesPromotedDepthEdges(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := createSchema(db); err != nil {
		t.Fatal(err)
	}

	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()

	// node 3 (class Account) lives in the file being reindexed (src/models.py);
	// node 1 (a genuine cross-file caller) and node 2 (a method that READS Account)
	// both live elsewhere and survive the reindex delete.
	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, file_path, language) VALUES
		 (1, 'Function', 'caller',  'src/caller.py',  'python'),
		 (2, 'Method',   'reader',  'src/service.py', 'python'),
		 (3, 'Class',    'Account', 'src/models.py',  'python'),
		 (4, 'Method',   'save',    'src/models.py',  'python')`,
	); err != nil {
		t.Fatal(err)
	}

	// Two INCOMING cross-file edges targeting nodes in src/models.py:
	//   (a) a genuine CALLS edge caller -> Account.save (must be snapshotted), and
	//   (b) a promoted READS depth edge reader -> Account (must be EXCLUDED).
	if _, err := tx.Exec(
		`INSERT INTO edges
		   (source_id, target_id, type, source_line, source_file, resolution_method, confidence, trust_tier, candidate_count, evidence_type, verification_status)
		 VALUES
		   (1, 4, 'CALLS', 10, 'src/caller.py',  'import',             1.0, 'CERTIFIED', 1, 'ast_call',   'unverified'),
		   (2, 3, 'READS', 22, 'src/service.py', 'promote_field_read', 0.9, 'CERTIFIED', 1, 'field_read', 'unverified')`,
	); err != nil {
		t.Fatal(err)
	}

	snap, err := SnapshotIncomingEdgesTx(tx, "src/models.py", 0)
	if err != nil {
		t.Fatal(err)
	}

	// Exactly ONE edge — the genuine CALLS — may be snapshotted. The promoted READS
	// edge must be excluded (the whole-graph promote pass regenerates it post-commit).
	if len(snap) != 1 {
		methods := make([]string, len(snap))
		for i, r := range snap {
			methods[i] = r.EdgeType + ":" + r.ResolutionMethod
		}
		t.Fatalf("snapshot len=%d %v; expected exactly 1 (the CALLS edge), the promote_* READS edge must be excluded", len(snap), methods)
	}
	if snap[0].EdgeType != "CALLS" || snap[0].ResolutionMethod != "import" {
		t.Fatalf("snapshotted edge = %q/%q; expected the genuine CALLS/import edge", snap[0].EdgeType, snap[0].ResolutionMethod)
	}
	for _, r := range snap {
		if strings.HasPrefix(r.ResolutionMethod, "promote_") {
			t.Fatalf("a promoted depth edge (%s) was snapshotted — it will be restored as a name_match phantom and duplicated by the post-commit promote pass", r.ResolutionMethod)
		}
	}
}

// B6 — an incoming edge's metadata (e.g. api_edges route info) must SURVIVE a -file
// reindex: SnapshotIncomingEdgesTx must carry it and ResolveIncomingEdgesTx must write it
// back verbatim. Before the fix the snapshot never selected metadata and the restore INSERT
// hardcoded metadata=NULL → the column was erased on every incremental reindex.
// Mutation: not selecting metadata (RED at snap check) / restoring NULL (RED at restore check).
func TestResolveIncomingEdgesPreservesMetadata(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := createSchema(db); err != nil {
		t.Fatal(err)
	}
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()

	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, file_path, language) VALUES
		 (1, 'Function', 'caller', 'src/caller.py', 'python'),
		 (4, 'Method',   'save',   'src/models.py', 'python')`,
	); err != nil {
		t.Fatal(err)
	}
	const wantMeta = "route:GET /x|sig:save(self)"
	if _, err := tx.Exec(
		`INSERT INTO edges
		   (source_id, target_id, type, source_line, source_file, resolution_method, confidence, metadata, trust_tier, candidate_count, evidence_type, verification_status)
		 VALUES (1, 4, 'CALLS', 10, 'src/caller.py', 'import', 1.0, ?, 'CERTIFIED', 1, 'ast_call', 'unverified')`,
		wantMeta,
	); err != nil {
		t.Fatal(err)
	}

	snap, err := SnapshotIncomingEdgesTx(tx, "src/models.py", 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(snap) != 1 {
		t.Fatalf("snapshot len=%d; expected 1", len(snap))
	}
	if snap[0].Metadata != wantMeta {
		t.Fatalf("snapshot dropped metadata: got %q want %q", snap[0].Metadata, wantMeta)
	}

	// Simulate the reindex: delete the target node + its edges, re-insert `save`
	// under a fresh id, then restore the snapshotted incoming edge.
	if _, err := tx.Exec(`DELETE FROM edges WHERE target_id = 4`); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(`DELETE FROM nodes WHERE id = 4`); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, file_path, language) VALUES (5, 'Method', 'save', 'src/models.py', 'python')`,
	); err != nil {
		t.Fatal(err)
	}
	if _, _, err := ResolveIncomingEdgesTx(tx, snap, "src/models.py"); err != nil {
		t.Fatal(err)
	}
	var gotMeta string
	if err := tx.QueryRow(`SELECT COALESCE(metadata, '') FROM edges WHERE source_id = 1 AND target_id = 5`).Scan(&gotMeta); err != nil {
		t.Fatal(err)
	}
	if gotMeta != wantMeta {
		t.Fatalf("restored edge erased metadata: got %q want %q", gotMeta, wantMeta)
	}
}

// TestResolveIncomingEdgesDemotesVerifiedUniqueOnRenameReplace is the BITING test for
// the P0 incremental-restore laundering fix. A cross-file edge resolved as
// `verified_unique` (conf 0.95, CERTIFIED) — a tier earned by GLOBAL UNIQUENESS of the
// target's qualified_name at full-index time — targets `process` in src/target.py. The
// file is reindexed and `process` is RENAMED-AND-REPLACED: the reindexed file now has
// exactly ONE node named `process`, but it is a DIFFERENT symbol (qualified_name
// "src.target.NewClass.process" vs the original "src.target.OldClass.process"). The
// bare-name re-match finds that single node; before the fix it preserved the original
// verified_unique/0.95/CERTIFIED tier VERBATIM onto the wrong node. After the fix, the
// absence of an exact qualified_name re-match caps the restore at CANDIDATE (0.6).
func TestResolveIncomingEdgesDemotesVerifiedUniqueOnRenameReplace(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := createSchema(db); err != nil {
		t.Fatal(err)
	}

	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()

	// Post-reindex state: caller survives; src/target.py now holds a DIFFERENT `process`
	// whose qualified_name differs from the snapshot's original target.
	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, qualified_name, file_path, language) VALUES
		 (1, 'Function', 'caller',  'src.caller.caller',          'src/caller.py', 'python'),
		 (2, 'Method',   'process', 'src.target.NewClass.process','src/target.py', 'python')`,
	); err != nil {
		t.Fatal(err)
	}

	// Snapshot of the ORIGINAL verified_unique edge: its target's qualified_name was
	// the OLD class's process, which no longer exists after the rename-replace.
	snap := []IncomingEdgeRef{{
		SourceID:            1,
		SourceLine:          7,
		EdgeType:            "CALLS",
		SourceFile:          "src/caller.py",
		TargetName:          "process",
		ResolutionMethod:    "verified_unique",
		Confidence:          0.95,
		EvidenceType:        "verified_unique",
		TargetQualifiedName: "src.target.OldClass.process",
	}}

	restored, unresolved, err := ResolveIncomingEdgesTx(tx, snap, "src/target.py")
	if err != nil {
		t.Fatal(err)
	}
	if restored != 1 || unresolved != 0 {
		t.Fatalf("restored=%d unresolved=%d", restored, unresolved)
	}

	var method, tier string
	var confidence float64
	if err := tx.QueryRow(
		`SELECT resolution_method, trust_tier, confidence FROM edges WHERE source_id = 1 AND target_id = 2`,
	).Scan(&method, &tier, &confidence); err != nil {
		t.Fatal(err)
	}
	// THE BITE: a bare-name re-match with NO qualified_name re-proof must NOT preserve
	// the verified_unique CERTIFIED tier onto the renamed-replaced node.
	if tier == "CERTIFIED" || confidence > 0.6 {
		t.Fatalf("laundered: verified_unique restored at tier=%q conf=%v onto a node whose qualified_name does not match the original — must cap at CANDIDATE 0.6", tier, confidence)
	}
	if confidence != 0.6 || tier != "CANDIDATE" {
		t.Fatalf("demoted restore want conf=0.6 CANDIDATE, got conf=%v tier=%q", confidence, tier)
	}
}

// TestResolveIncomingEdgesPreservesVerifiedUniqueOnQNameMatch is the COMPLEMENT: when
// the reindexed file still contains the SAME symbol (qualified_name unchanged), the
// verified_unique tier is legitimately re-proven and PRESERVED at CERTIFIED. This pins
// that the demotion above is targeted (wrong-target only), not a blanket teardown of
// every deterministic restore.
func TestResolveIncomingEdgesPreservesVerifiedUniqueOnQNameMatch(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := createSchema(db); err != nil {
		t.Fatal(err)
	}

	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()

	// The reindexed file still holds the SAME process (qualified_name unchanged).
	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, qualified_name, file_path, language) VALUES
		 (1, 'Function', 'caller',  'src.caller.caller',          'src/caller.py', 'python'),
		 (2, 'Method',   'process', 'src.target.OldClass.process','src/target.py', 'python')`,
	); err != nil {
		t.Fatal(err)
	}

	snap := []IncomingEdgeRef{{
		SourceID:            1,
		SourceLine:          7,
		EdgeType:            "CALLS",
		SourceFile:          "src/caller.py",
		TargetName:          "process",
		ResolutionMethod:    "verified_unique",
		Confidence:          0.95,
		EvidenceType:        "verified_unique",
		TargetQualifiedName: "src.target.OldClass.process",
	}}

	if _, _, err := ResolveIncomingEdgesTx(tx, snap, "src/target.py"); err != nil {
		t.Fatal(err)
	}

	var method, tier string
	var confidence float64
	if err := tx.QueryRow(
		`SELECT resolution_method, trust_tier, confidence FROM edges WHERE source_id = 1 AND target_id = 2`,
	).Scan(&method, &tier, &confidence); err != nil {
		t.Fatal(err)
	}
	if method != "verified_unique" || tier != "CERTIFIED" || confidence != 0.95 {
		t.Fatalf("qname-matched restore must preserve verified_unique/CERTIFIED/0.95, got method=%q tier=%q conf=%v", method, tier, confidence)
	}
}
