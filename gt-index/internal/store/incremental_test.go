package store

import (
	"database/sql"
	"path/filepath"
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
	if confidence != 0.9 {
		t.Fatalf("confidence=%v", confidence)
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
	if confidence >= 0.95 {
		t.Fatalf("confidence=%v; demoted edge must not regain CERTIFIED-level confidence", confidence)
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
