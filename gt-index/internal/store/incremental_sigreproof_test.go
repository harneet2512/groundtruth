package store

import (
	"database/sql"
	"path/filepath"
	"testing"
)

// R#2 REGRESSION GUARD (the LIPI #1 residual fix) — signature as a second identity
// witness. When the reindexed file holds 2+ same-named methods whose qualified_name
// is the BARE name (idiomatic Go cross-file-receiver methods: `types.go` declares the
// type, `methods.go` defines the method, so linkGoReceiverMethods cannot receiver-
// qualify them and they collide on qualified_name), the exact-qname re-match is
// ambiguous (qnameMatchCount>1) and the pre-R#2 restore stripped the incoming
// type-aware edge to name_match@ids[0] — L6 making the map WORSE post-edit (the
// verified caller vanished from the contract pillar). A UNIQUE signature match (the
// signature embeds the receiver: `func (b *Beta) Reset()`) re-proves WHICH node the
// edge meant, so the tier is preserved onto the RIGHT node. RED before the R#2 witness
// (target=ids[0]=Alpha, method=name_match), GREEN after (target=Beta, method=lsp).
func TestIncremental_SignatureReproof_PreservesTierWhenQnameAmbiguous(t *testing.T) {
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

	// methods.go holds TWO methods named `Reset` (receivers Alpha and Beta declared
	// cross-file in types.go), BOTH with the bare qualified_name "Reset" — but their
	// signatures differ (each embeds its receiver). Node 2 (Alpha) is ids[0], the WRONG
	// target the pre-fix name_match guess picks; node 3 (Beta) is the real target.
	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, qualified_name, signature, file_path, language) VALUES
		 (1, 'Function', 'Use',   'pkg.caller.Use', 'func Use(b *Beta)',       'pkg/caller.go',  'go'),
		 (2, 'Method',   'Reset', 'Reset',          'func (a *Alpha) Reset()', 'pkg/methods.go', 'go'),
		 (3, 'Method',   'Reset', 'Reset',          'func (b *Beta) Reset()',  'pkg/methods.go', 'go')`,
	); err != nil {
		t.Fatal(err)
	}

	// The full index resolved this call to Beta.Reset via the receiver type —
	// lsp/0.95/CERTIFIED — carrying Beta's signature as the identity witness. The
	// qualified_name is bare "Reset", so it collides with Alpha.Reset (NOT unique).
	snap := []IncomingEdgeRef{{
		SourceID:            1,
		SourceLine:          7,
		EdgeType:            "CALLS",
		SourceFile:          "pkg/caller.go",
		TargetName:          "Reset",
		TargetQualifiedName: "Reset",
		TargetSignature:     "func (b *Beta) Reset()",
		ResolutionMethod:    "lsp",
		Confidence:          0.95,
		EvidenceType:        "lsp_definition",
	}}
	restored, unresolved, err := ResolveIncomingEdgesTx(tx, snap, "pkg/methods.go")
	if err != nil {
		t.Fatal(err)
	}
	if restored != 1 || unresolved != 0 {
		t.Fatalf("restored=%d unresolved=%d", restored, unresolved)
	}

	var method, tier, evidenceType string
	var confidence float64
	var targetID int64
	if err := tx.QueryRow(
		`SELECT target_id, resolution_method, trust_tier, evidence_type, confidence
		   FROM edges WHERE source_id = 1`,
	).Scan(&targetID, &method, &tier, &evidenceType, &confidence); err != nil {
		t.Fatal(err)
	}
	t.Logf("RESULT: target_id=%d method=%q tier=%q evidence=%q conf=%v",
		targetID, method, tier, evidenceType, confidence)
	// The UNIQUE signature match picks node 3 (Beta.Reset), NOT ids[0]=node 2 (Alpha).
	if targetID != 3 {
		t.Fatalf("targetID=%d; expected the UNIQUE signature re-match to pick node 3 (Beta.Reset), not ids[0]=2 (Alpha.Reset)", targetID)
	}
	// And the type-aware tier is preserved (lsp is type-derived, exempt from the
	// uniqueness cap): the verified caller survives the reindex.
	if method != "lsp" || evidenceType != "lsp_definition" || confidence != 0.95 || tier != "CERTIFIED" {
		t.Fatalf("R#2 REGRESSED: a signature-re-matched lsp/0.95 edge downgraded to method=%q evidence=%q conf=%v tier=%q — identity was re-proven by signature; the tier must be preserved",
			method, evidenceType, confidence, tier)
	}
}

// R#2 SAFETY GUARD: two same-named nodes that ALSO share an IDENTICAL signature
// (a genuine @overload / duplicate stub) do NOT uniquely re-prove identity — the
// signature witness must NOT preserve a CERTIFIED tier onto the arbitrary first.
// Correct-or-quiet: falls to the name_match guess, exactly like the duplicate-qname
// case. This pins that R#2 never OVER-preserves when the signature is also ambiguous.
func TestIncremental_IdenticalSignature_DoesNotLaunder(t *testing.T) {
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

	// util.go holds TWO `Reset` defs, bare qname "Reset", with the SAME signature.
	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, qualified_name, signature, file_path, language) VALUES
		 (1, 'Function', 'Use',   'pkg.caller.Use', 'func Use()',   'pkg/caller.go', 'go'),
		 (2, 'Function', 'Reset', 'Reset',          'func Reset()', 'pkg/util.go',   'go'),
		 (3, 'Function', 'Reset', 'Reset',          'func Reset()', 'pkg/util.go',   'go')`,
	); err != nil {
		t.Fatal(err)
	}

	snap := []IncomingEdgeRef{{
		SourceID:            1,
		SourceLine:          5,
		EdgeType:            "CALLS",
		SourceFile:          "pkg/caller.go",
		TargetName:          "Reset",
		TargetQualifiedName: "Reset",
		TargetSignature:     "func Reset()", // matches BOTH candidates → count>1, NOT unique
		ResolutionMethod:    "lsp",
		Confidence:          0.95,
		EvidenceType:        "lsp_definition",
	}}
	restored, _, err := ResolveIncomingEdgesTx(tx, snap, "pkg/util.go")
	if err != nil {
		t.Fatal(err)
	}
	if restored != 1 {
		t.Fatalf("restored=%d, want 1 (edge kept as a guess, not dropped)", restored)
	}
	var method, tier string
	var confidence float64
	if err := tx.QueryRow(
		`SELECT resolution_method, trust_tier, confidence FROM edges WHERE source_id = 1`,
	).Scan(&method, &tier, &confidence); err != nil {
		t.Fatal(err)
	}
	t.Logf("RESULT: method=%q tier=%q conf=%v", method, tier, confidence)
	// An ambiguous signature is not a unique identity re-proof → lsp/0.95/CERTIFIED must
	// NOT be laundered onto the arbitrary first `Reset`. Correct-or-quiet: name_match.
	if method == "lsp" || confidence >= 0.95 || tier == "CERTIFIED" {
		t.Fatalf("OVER-PRESERVED: an identical-signature reindex re-certified lsp/0.95/CERTIFIED onto an arbitrary node (method=%q tier=%q conf=%v) — signature was NOT uniquely re-proven",
			method, tier, confidence)
	}
}
