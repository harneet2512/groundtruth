package store

import (
	"database/sql"
	"path/filepath"
	"strings"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// The engine half of hybrid retrieval (gt_engine/retrieval.py) reaches the
// behavioural facts through `properties`: `lexical_rank` gets BM25 out of
// `nodes_fts`, but `property_rank` has no index at all and falls back to a
// LIKE scan over every row. These tests pin the producer side of that: an
// FTS5 index over `properties` that is created with the schema, maintained by
// the same transaction that writes the rows, and keyed so a hit maps back to
// `properties.id` and to the owning symbol's minted `stable_id`.

// propertyFixture builds the smallest graph that carries a behavioural fact:
// two symbols with minted stable ids, guard clauses on both, and the
// `resolution_symbols` rows that make the `native_id = nodes.id` path real.
func propertyFixture(t *testing.T) *DB {
	t.Helper()
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.db.Exec(
		`INSERT INTO nodes (id, stable_id, label, name, qualified_name, file_path, language) VALUES
		 (1, 'sym:parse', 'Function', 'parseInput', 'mod.parseInput', 'src/parse.ts', 'typescript'),
		 (2, 'sym:emit',  'Function', 'emitOutput', 'mod.emitOutput', 'src/emit.ts',  'typescript')`,
	); err != nil {
		t.Fatal(err)
	}
	if _, err := db.db.Exec(
		`INSERT INTO resolution_symbols
		 (stable_id, native_id, native_kind, normalized_kind, language, path, qualified_name, start_line, end_line, export_status)
		 VALUES
		 ('sym:parse', '1', 'Function', 'function', 'typescript', 'src/parse.ts', 'mod.parseInput', 1, 9, 'exported'),
		 ('sym:emit',  '2', 'Function', 'function', 'typescript', 'src/emit.ts',  'mod.emitOutput', 1, 9, 'exported')`,
	); err != nil {
		t.Fatal(err)
	}
	props := []*Property{
		{NodeID: 1, Kind: "guard_clause", Value: "validates empty input before parse", Line: 3, Confidence: 0.9},
		{NodeID: 1, Kind: "return_shape", Value: "returns parsed node tree", Line: 7, Confidence: 0.8},
		{NodeID: 2, Kind: "guard_clause", Value: "rejects unknown writer target", Line: 4, Confidence: 0.7},
	}
	if err := db.BatchInsertProperties(props); err != nil {
		t.Fatal(err)
	}
	return db
}

func scalarInt(t *testing.T, db *sql.DB, query string, args ...interface{}) int {
	t.Helper()
	var n int
	if err := db.QueryRow(query, args...).Scan(&n); err != nil {
		t.Fatalf("query %q: %v", query, err)
	}
	return n
}

// TestPropertiesFTSTableIsCreatedWithTheSchema: the index must exist for every
// graph the producer writes, exactly as nodes_fts does. A graph that carries
// `properties` but no `properties_fts` silently degrades property_rank back to
// the full scan this item is replacing.
func TestPropertiesFTSTableIsCreatedWithTheSchema(t *testing.T) {
	db, err := sql.Open("sqlite3", filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := createSchema(db); err != nil {
		t.Fatal(err)
	}
	if n := scalarInt(t, db,
		`SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='properties_fts'`,
	); n != 1 {
		t.Fatalf("properties_fts not created by createSchema (found %d); property_rank stays a full scan", n)
	}
}

// TestPropertiesFTSIsExternalContentOverProperties: nodes_fts is declared
// content='nodes', content_rowid='id'. The properties index has to make the
// same choice, because that is what lets a hit carry `properties.id` in its
// rowid instead of duplicating every value into a second copy of the table.
func TestPropertiesFTSIsExternalContentOverProperties(t *testing.T) {
	db, err := sql.Open("sqlite3", filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := createSchema(db); err != nil {
		t.Fatal(err)
	}
	var ddl string
	if err := db.QueryRow(
		`SELECT sql FROM sqlite_master WHERE type='table' AND name='properties_fts'`,
	).Scan(&ddl); err != nil {
		t.Fatalf("properties_fts has no DDL to inspect: %v", err)
	}
	normalized := strings.ToLower(strings.Join(strings.Fields(ddl), " "))
	for _, want := range []string{"using fts5", "content='properties'", "content_rowid='id'"} {
		if !strings.Contains(normalized, want) {
			t.Errorf("properties_fts DDL is missing %q: %s", want, normalized)
		}
	}
}

// TestPropertiesFTSCoversEveryPropertyRow: the acceptance number. An index
// that covers a subset is worse than no index, because the ranking it
// produces looks complete and is not.
func TestPropertiesFTSCoversEveryPropertyRow(t *testing.T) {
	db := propertyFixture(t)
	properties := scalarInt(t, db.db, `SELECT COUNT(*) FROM properties`)
	if properties != 3 {
		t.Fatalf("fixture wrote %d properties, want 3", properties)
	}
	indexed := scalarInt(t, db.db, `SELECT COUNT(*) FROM properties_fts`)
	if indexed != properties {
		t.Fatalf("properties_fts has %d rows, properties has %d -- the index does not cover the table", indexed, properties)
	}
}

// TestPropertiesFTSMatchReachesTheOwningSymbol: the whole point of delta row 4.
// "validates empty input" appears in no identifier anywhere; it exists only in
// a guard_clause value. The hit is on the property row and the answer is the
// symbol that owns it, reached through properties.id -> nodes.id -> stable_id.
func TestPropertiesFTSMatchReachesTheOwningSymbol(t *testing.T) {
	db := propertyFixture(t)
	rows, err := db.db.Query(
		`SELECT n.stable_id, p.id, p.kind
		   FROM properties_fts f
		   JOIN properties p ON p.id = f.rowid
		   JOIN nodes n ON n.id = p.node_id
		  WHERE properties_fts MATCH '"validates"'
		  ORDER BY p.id`,
	)
	if err != nil {
		t.Fatalf("MATCH over properties_fts failed: %v", err)
	}
	defer rows.Close()
	type hit struct {
		stableID string
		propID   int64
		kind     string
	}
	var hits []hit
	for rows.Next() {
		var h hit
		if err := rows.Scan(&h.stableID, &h.propID, &h.kind); err != nil {
			t.Fatal(err)
		}
		hits = append(hits, h)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if len(hits) != 1 {
		t.Fatalf("got %d hits for a term that occurs in exactly one guard_clause, want 1: %+v", len(hits), hits)
	}
	if hits[0].stableID != "sym:parse" {
		t.Errorf("hit resolved to stable_id %q, want sym:parse", hits[0].stableID)
	}
	if hits[0].kind != "guard_clause" {
		t.Errorf("hit resolved to kind %q, want guard_clause", hits[0].kind)
	}
	var native string
	if err := db.db.QueryRow(
		`SELECT rs.native_id FROM resolution_symbols rs
		   JOIN properties p ON CAST(rs.native_id AS INTEGER) = p.node_id
		  WHERE p.id = ?`, hits[0].propID,
	).Scan(&native); err != nil {
		t.Fatalf("hit does not reach resolution_symbols.native_id: %v", err)
	}
	if native != "1" {
		t.Errorf("native_id %q, want 1", native)
	}
}

// TestPropertiesFTSFiltersByKindColumn: property_rank already narrows by
// p.kind. The index has to keep that reachable as an FTS column filter, so a
// kind-scoped behavioural query never falls back to the scan either.
func TestPropertiesFTSFiltersByKindColumn(t *testing.T) {
	db := propertyFixture(t)
	n := scalarInt(t, db.db,
		`SELECT COUNT(*) FROM properties_fts WHERE properties_fts MATCH '{value} : "returns"'`)
	if n != 1 {
		t.Fatalf("column-filtered MATCH on {value} returned %d rows, want 1", n)
	}
	k := scalarInt(t, db.db,
		`SELECT COUNT(*) FROM properties_fts WHERE properties_fts MATCH '{kind} : "guard_clause"'`)
	if k != 2 {
		t.Fatalf("column-filtered MATCH on {kind} returned %d rows, want 2", k)
	}
}

// TestPropertiesFTSSurvivesTheIncrementalDeleteReinsert: the incremental path
// deletes a file's properties and writes fresh ones inside one transaction.
// An external-content index that is not maintained in that same transaction
// goes stale exactly the way nodes_fts has repeatedly gone stale -- COUNT
// still looks right while MATCH returns rows that no longer exist.
func TestPropertiesFTSSurvivesTheIncrementalDeleteReinsert(t *testing.T) {
	db := propertyFixture(t)
	tx, err := db.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(
		`DELETE FROM properties WHERE node_id IN (SELECT id FROM nodes WHERE file_path = ?)`,
		"src/parse.ts",
	); err != nil {
		tx.Rollback()
		t.Fatal(err)
	}
	if err := BatchInsertPropertiesTx(tx, []*Property{
		{NodeID: 1, Kind: "guard_clause", Value: "validates trailing separator", Line: 3, Confidence: 0.9},
	}); err != nil {
		tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}

	properties := scalarInt(t, db.db, `SELECT COUNT(*) FROM properties`)
	indexed := scalarInt(t, db.db, `SELECT COUNT(*) FROM properties_fts`)
	if indexed != properties {
		t.Fatalf("after incremental delete+reinsert: properties_fts=%d properties=%d", indexed, properties)
	}
	stale := scalarInt(t, db.db,
		`SELECT COUNT(*) FROM properties_fts WHERE properties_fts MATCH '"input"'`)
	if stale != 0 {
		t.Errorf("index still matches %d rows for a value that was deleted", stale)
	}
	fresh := scalarInt(t, db.db,
		`SELECT COUNT(*) FROM properties_fts WHERE properties_fts MATCH '"separator"'`)
	if fresh != 1 {
		t.Errorf("index matches %d rows for the replacement value, want 1", fresh)
	}
}

// TestPropertiesFTSGrowsByExactlyTheRowsWritten: the producer writes
// properties in several batches (definitions, then serde pairs, then
// structural twins). Each batch must add its own rows and only its own rows;
// an index rebuilt without clearing double-counts, and the first thing a
// reader computes -- how much evidence a symbol has -- is then wrong.
func TestPropertiesFTSGrowsByExactlyTheRowsWritten(t *testing.T) {
	db := propertyFixture(t)
	before := scalarInt(t, db.db, `SELECT COUNT(*) FROM properties_fts`)
	if err := db.BatchInsertProperties([]*Property{
		{NodeID: 2, Kind: "return_shape", Value: "returns emitted bytes", Line: 8, Confidence: 0.6},
	}); err != nil {
		t.Fatal(err)
	}
	after := scalarInt(t, db.db, `SELECT COUNT(*) FROM properties_fts`)
	if after != before+1 {
		t.Fatalf("a one-row batch moved properties_fts %d -> %d", before, after)
	}
	if properties := scalarInt(t, db.db, `SELECT COUNT(*) FROM properties`); after != properties {
		t.Fatalf("properties_fts=%d properties=%d after a second batch", after, properties)
	}
}
