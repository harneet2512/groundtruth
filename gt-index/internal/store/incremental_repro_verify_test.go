package store

import (
	"database/sql"
	"path/filepath"
	"testing"
)

// R#1 REGRESSION GUARD (was an adversarial bug-repro): when the reindexed file
// contains 2+ same-named nodes but the incoming edge's exact qualified_name IS
// re-matched, the incremental restore must PRESERVE the original deterministic tier
// (lsp/0.95/CERTIFIED onto the RIGHT node), NOT downgrade it to a name_match/0.6
// guess. Pre-R1 the len(ids)==1 gate downgraded it the moment a second same-named
// method appeared, silently dropping a verified caller from the contract pillar
// post-edit. RED before the incremental.go:276 fix, GREEN after.
func TestIncremental_QnameReproof_PreservesLSPTierWhenAmbiguous(t *testing.T) {
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

	// models.py holds TWO methods named `save` (class A and class B).
	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, qualified_name, file_path, language) VALUES
		 (1, 'Function', 'caller', 'src.caller.caller',   'src/caller.py', 'python'),
		 (2, 'Method',   'save',   'src.models.A.save',   'src/models.py', 'python'),
		 (3, 'Method',   'save',   'src.models.B.save',   'src/models.py', 'python')`,
	); err != nil {
		t.Fatal(err)
	}

	snap := []IncomingEdgeRef{{
		SourceID:            1,
		SourceLine:          12,
		EdgeType:            "CALLS",
		SourceFile:          "src/caller.py",
		TargetName:          "save",
		TargetQualifiedName: "src.models.A.save",
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
	var targetID int64
	if err := tx.QueryRow(
		`SELECT target_id, resolution_method, trust_tier, evidence_type, confidence
		   FROM edges WHERE source_id = 1`,
	).Scan(&targetID, &method, &tier, &evidenceType, &confidence); err != nil {
		t.Fatal(err)
	}
	t.Logf("RESULT: target_id=%d method=%q tier=%q evidence=%q conf=%v",
		targetID, method, tier, evidenceType, confidence)
	// Correct (post-R1) outcome: the exact qualified_name re-match picks node 2 (A.save)
	// AND the original lsp/0.95/CERTIFIED tier is PRESERVED — the verified caller survives
	// the reindex even though models.py now holds two `save` methods.
	if targetID != 2 {
		t.Fatalf("targetID=%d; expected the exact qname re-match to pick node 2 (A.save)", targetID)
	}
	if method != "lsp" || evidenceType != "lsp_definition" || confidence != 0.95 {
		t.Fatalf("R1 REGRESSED: an exact-qname-re-matched lsp/0.95 edge downgraded to method=%q evidence=%q conf=%v — the deterministic tier must be preserved when identity is re-proven",
			method, evidenceType, confidence)
	}
	if tier != "CERTIFIED" {
		t.Fatalf("tier=%q, want CERTIFIED (conf 0.95) — a re-proven LSP edge stays CERTIFIED", tier)
	}
}

// R#1 / Fable #2 GUARD: when the reindexed file holds 2+ nodes that SHARE a
// qualified_name (Python @overload stacks / platform-conditional defs — qualified_name
// is bare `name` for top-level funcs, so it is NOT unique in a file), the exact-qname
// re-match does not re-prove WHICH node the edge meant. The restore must NOT preserve a
// CERTIFIED lsp tier onto the arbitrary first — it must fall to the name_match guess.
// RED before the qnameMatchCount==1 gate (which laundered lsp/0.95 onto the first stub).
func TestIncremental_DuplicateQname_DoesNotLaunderCertifiedTier(t *testing.T) {
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

	// mod.py holds TWO top-level `f` defs (an @overload stub + the impl), BOTH with
	// qualified_name "app.mod.f".
	if _, err := tx.Exec(
		`INSERT INTO nodes (id, label, name, qualified_name, file_path, language) VALUES
		 (1, 'Function', 'caller', 'app.caller.caller', 'app/caller.py', 'python'),
		 (2, 'Function', 'f',      'app.mod.f',         'app/mod.py',    'python'),
		 (3, 'Function', 'f',      'app.mod.f',         'app/mod.py',    'python')`,
	); err != nil {
		t.Fatal(err)
	}

	snap := []IncomingEdgeRef{{
		SourceID:            1,
		SourceLine:          9,
		EdgeType:            "CALLS",
		SourceFile:          "app/caller.py",
		TargetName:          "f",
		TargetQualifiedName: "app.mod.f",
		ResolutionMethod:    "lsp",
		Confidence:          0.95,
		EvidenceType:        "lsp_definition",
	}}
	restored, _, err := ResolveIncomingEdgesTx(tx, snap, "app/mod.py")
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
	// A duplicate qname is not a unique identity re-proof → lsp/0.95/CERTIFIED must NOT
	// be laundered onto the arbitrary first `f`. Correct-or-quiet: a name_match guess.
	if method == "lsp" || confidence >= 0.95 || tier == "CERTIFIED" {
		t.Fatalf("LAUNDERED: a duplicate-qname reindex re-certified lsp/0.95/CERTIFIED onto an arbitrary node (method=%q tier=%q conf=%v) — identity was NOT uniquely re-proven",
			method, tier, confidence)
	}
}
