package store

import (
	"database/sql"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// b22TestDB opens a fresh schema'd DB (incl. the B-22 property-provenance migration).
func b22TestDB(t *testing.T) *DB {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	sqldb, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := createSchema(sqldb); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqldb.Close() })
	return &DB{db: sqldb}
}

// B-22 (properties provenance): the properties table must carry per-fact provenance —
// property_id, start_line, end_line, extractor, evidence_method, trust_tier,
// verification_status, source_revision — not just (node_id, kind, value, line, confidence).
// The writer must POPULATE them deterministically at extraction time.
//
// RED on current code: the SELECT of the new columns errors ("no such column: property_id").
func TestB22_PropertyProvenanceColumnsPopulated(t *testing.T) {
	d := b22TestDB(t)
	if _, err := d.db.Exec(
		`INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language)
		 VALUES (1, 'Function', 'get_user', 'src/u.py', 10, 20, 'python')`,
	); err != nil {
		t.Fatal(err)
	}
	// conf 0.6 -> CANDIDATE (mirrors edges' tierFor thresholds).
	if err := d.InsertProperty(&Property{NodeID: 1, Kind: "guard_clause", Value: "if x is None", Line: 12, Confidence: 0.6}); err != nil {
		t.Fatal(err)
	}

	var (
		propID, extractor, evMethod, tier, vstatus, srcRev string
		startLine, endLine                                 int
	)
	err := d.db.QueryRow(
		`SELECT property_id, start_line, end_line, extractor, evidence_method,
		        trust_tier, verification_status, COALESCE(source_revision,'')
		   FROM properties WHERE node_id = 1`,
	).Scan(&propID, &startLine, &endLine, &extractor, &evMethod, &tier, &vstatus, &srcRev)
	if err != nil {
		t.Fatalf("B-22 provenance columns not readable (schema not extended?): %v", err)
	}
	if len(propID) != 64 {
		t.Errorf("property_id is not a stable 64-hex content id: %q (len %d)", propID, len(propID))
	}
	if startLine != 12 {
		t.Errorf("start_line=%d want 12 (defaults to the property line)", startLine)
	}
	if endLine != 12 {
		t.Errorf("end_line=%d want 12 (point fact defaults to the property line)", endLine)
	}
	if extractor == "" {
		t.Errorf("extractor is empty — no provenance of which extractor produced the fact")
	}
	if evMethod == "" {
		t.Errorf("evidence_method is empty — no evidence provenance")
	}
	if tier != "CANDIDATE" {
		t.Errorf("trust_tier=%q want CANDIDATE for confidence 0.6 (parity with edges' tierFor)", tier)
	}
	if vstatus != "unverified" {
		t.Errorf("verification_status=%q want unverified (default)", vstatus)
	}
}

// B-22 stable-id discrimination: two properties with different content must get different
// property_ids; the id is a deterministic function of the fact content.
func TestB22_PropertyIDDeterministicAndDistinct(t *testing.T) {
	d := b22TestDB(t)
	if _, err := d.db.Exec(
		`INSERT INTO nodes (id, label, name, file_path, language) VALUES (1,'Function','f','a.py','python')`,
	); err != nil {
		t.Fatal(err)
	}
	if err := d.InsertProperty(&Property{NodeID: 1, Kind: "return_shape", Value: "Optional[User]", Line: 3, Confidence: 1.0}); err != nil {
		t.Fatal(err)
	}
	if err := d.InsertProperty(&Property{NodeID: 1, Kind: "return_shape", Value: "User", Line: 3, Confidence: 1.0}); err != nil {
		t.Fatal(err)
	}
	rows, err := d.db.Query(`SELECT property_id FROM properties ORDER BY id`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			t.Fatal(err)
		}
		ids = append(ids, id)
	}
	if len(ids) != 2 {
		t.Fatalf("want 2 property rows, got %d", len(ids))
	}
	if ids[0] == ids[1] {
		t.Errorf("distinct property values produced the same property_id: %q", ids[0])
	}
	// conf 1.0 -> CERTIFIED
	var tier string
	d.db.QueryRow(`SELECT trust_tier FROM properties WHERE id = 1`).Scan(&tier)
	if tier != "CERTIFIED" {
		t.Errorf("trust_tier=%q want CERTIFIED for confidence 1.0", tier)
	}
}
