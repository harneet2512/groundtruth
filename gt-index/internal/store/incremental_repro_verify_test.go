package store

import (
	"database/sql"
	"path/filepath"
	"testing"
)

// ADVERSARIAL VERIFICATION REPRO (temporary — delete after run).
// Claim under test: when the reindexed file contains 2+ same-named nodes,
// an lsp/0.95 edge whose exact qualified_name IS re-proven still falls to
// the name_match guess ladder (method=name_match, evidence=name_match, 0.6).
func TestReproQnameReproofDeadWhenAmbiguous(t *testing.T) {
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
	// Assert the CLAIMED (buggy) outcome so a PASS = claim CONFIRMED.
	if targetID != 2 {
		t.Fatalf("targetID=%d; expected qname match to pick node 2 (A.save)", targetID)
	}
	if method != "name_match" || evidenceType != "name_match" || confidence != 0.6 {
		t.Fatalf("claim REFUTED: method=%q evidence=%q conf=%v (expected name_match/name_match/0.6 per claim)",
			method, evidenceType, confidence)
	}
}
