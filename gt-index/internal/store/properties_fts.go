package store

import (
	"database/sql"
	"fmt"
	"log"
)

// FTS5 over `properties` — the producer half of hybrid retrieval.
//
// `nodes_fts` answers "what is this called". It cannot answer "what does this
// do", because a behavioural query — "validates empty input", "raises on a
// closed writer" — has no reason to appear in any identifier. That text lives
// in `properties.value`: the guard clauses, return shapes, boundary
// conditions and exception flows the analysis passes already extract. There
// are 9,233 such rows on the reference graph against 3,511 code symbols, and
// until now not one of them was indexed. The read-only consumer
// (`gt_engine/retrieval.py.property_rank`) says so in its own docstring and
// falls back to `lower(p.value) LIKE '%term%'` — a full table scan, repeated
// once per query term, with no index to narrow it.
//
// This file adds `properties_fts` and nothing else. It is an index over rows
// that already exist: no new analysis, no new facts, and no promotion of
// candidate evidence to verified evidence. A symbol's rank changes; its
// trust tier does not.
//
// Design decisions, all taken to match `nodes_fts` rather than to invent a
// second convention:
//
//   - External content (`content='properties'`, `content_rowid='id'`). The
//     values are not duplicated into a second copy of the table — the index
//     stores only the inverted lists and reads the columns back through the
//     content table. That is what makes a hit's rowid *be* `properties.id`,
//     which is the join key the engine needs: `properties.id` →
//     `properties.node_id` → `nodes.id`, from which both `nodes.stable_id`
//     and `resolution_symbols.native_id` are one join away.
//   - Both indexed columns are real `properties` columns (`kind`, `value`),
//     which external content requires and which keeps `{kind} : "..."`
//     available as an FTS column filter — `property_rank` already narrows by
//     kind, and it should not have to leave the index to do it.
//   - Default (unicode61) tokenizer, the same as `nodes_fts`, so a term
//     tokenises identically whichever index it enters. This is a real
//     behavioural change for the consumer and is documented as such: word
//     matching is not substring matching (see PopulatePropertiesFTS5).
//   - Creation is non-fatal on its own, exactly as `nodes_fts` is, because
//     some SQLite builds lack FTS5. Failing closed on a *required* index is
//     the GT_REQUIRE_FTS5 gate's job, not the schema's.
//
// Maintenance uses FTS5's own `'rebuild'` command rather than the
// DELETE-then-INSERT that `PopulateFTS5` hand-rolls. `'rebuild'` discards the
// index and reconstructs it from the content table in one statement, so it
// cannot double-count, and — unlike DELETE — it does not need the old content
// rows to still be present in order to compute what to remove. That last
// property is what makes it safe in the incremental transaction, which
// deletes a file's properties before writing the replacements.

// propertiesFTSDDL is the single source of truth for the table's shape. It is
// used by creation and by the corruption-recovery path, so the two can never
// drift into indexing different columns.
const propertiesFTSDDL = `CREATE VIRTUAL TABLE %s properties_fts USING fts5(
	kind, value,
	content='properties', content_rowid='id'
)`

// propertiesFTSRebuild reconstructs the inverted index from `properties`.
// Idempotent by construction: FTS5 zeroes the index before it reads the
// content table, so running it twice is indistinguishable from running it
// once.
const propertiesFTSRebuild = `INSERT INTO properties_fts(properties_fts) VALUES('rebuild')`

// execer is the shared surface of *sql.DB and *sql.Tx that this file needs, so
// the same maintenance code runs on a connection during a full build and
// inside the publication transaction during an incremental one.
type execer interface {
	Exec(query string, args ...interface{}) (sql.Result, error)
	QueryRow(query string, args ...interface{}) *sql.Row
}

// createPropertiesFTS5 declares the index alongside the rest of the schema.
//
// Non-fatal, matching the `nodes_fts` precedent: a SQLite build without FTS5
// rejects the statement and the producer keeps going, leaving the engine on
// its LIKE fallback. A run that must not degrade silently is caught by the
// GT_REQUIRE_FTS5 preflight gate instead, which can see the difference
// between "absent" and "empty".
func createPropertiesFTS5(db execer) {
	if _, err := db.Exec(fmt.Sprintf(propertiesFTSDDL, "IF NOT EXISTS")); err != nil {
		log.Printf("[WARN] FTS5 over properties not available (non-fatal): %v", err)
	}
}

// propertiesFTSExists reports whether the virtual table is present. Absent
// means FTS5 was compiled out; it does not mean the index is empty.
func propertiesFTSExists(db execer) bool {
	var n int
	if err := db.QueryRow(
		`SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='properties_fts'`,
	).Scan(&n); err != nil {
		return false
	}
	return n > 0
}

// populatePropertiesFTS5 rebuilds the index from the current `properties`
// rows. Callers are the publication paths: the batch writers that put
// properties into the graph in the first place, and the incremental
// transaction that replaces a file's facts.
//
// The consumer-visible consequence, stated once here so it is not discovered
// by surprise: this index matches *tokens*, not substrings. `property_rank`'s
// current `LIKE '%parse%'` matches inside `UnitTypeParser`; `MATCH "parse"`
// does not. That asymmetry is deliberate and is the same one `lexical_rank`
// already lives with over `nodes_fts`, and it is why the fused ranking keeps
// both sources rather than replacing one with the other.
func populatePropertiesFTS5(db execer) error {
	if !propertiesFTSExists(db) {
		// FTS5 is compiled out. The engine's documented fallback handles it.
		return nil
	}
	if _, err := db.Exec(propertiesFTSRebuild); err == nil {
		return nil
	} else if recoverErr := recreatePropertiesFTS5(db, err); recoverErr != nil {
		return recoverErr
	}
	return nil
}

// recreatePropertiesFTS5 is the self-heal path. An external-content FTS5 index
// can be left structurally corrupt ("database disk image is malformed") when
// its shadow tables and its content table are separated — a graph.db copied
// away from its -wal between containers is the case that has actually bitten
// `nodes_fts` in production. `'rebuild'` then fails on the damaged shadow
// tables rather than repairing them, so the only recovery is to drop the
// virtual table, which drops the shadow tables with it, and declare it again.
func recreatePropertiesFTS5(db execer, cause error) error {
	log.Printf("[WARN] properties_fts rebuild failed (%v) — DROP+recreate to self-heal the FTS5 index", cause)
	if _, err := db.Exec(`DROP TABLE IF EXISTS properties_fts`); err != nil {
		return fmt.Errorf("drop corrupt properties_fts: %w", err)
	}
	if _, err := db.Exec(fmt.Sprintf(propertiesFTSDDL, "")); err != nil {
		return fmt.Errorf("recreate properties_fts: %w", err)
	}
	if _, err := db.Exec(propertiesFTSRebuild); err != nil {
		return fmt.Errorf("populate properties_fts after recreate: %w", err)
	}
	return nil
}

// PopulatePropertiesFTS5Tx maintains the index inside a caller's transaction.
// This is the incremental publication path: the same transaction deletes a
// file's properties, writes the replacements, and rebuilds the index, so the
// graph a reader sees after the commit can never have facts the index has not
// heard of — or index entries for facts that are gone.
func PopulatePropertiesFTS5Tx(tx *sql.Tx) error {
	return populatePropertiesFTS5(tx)
}

// PopulatePropertiesFTS5 maintains the index on the open database. Used by the
// full-build batch writers and by the rebuild-closure pass, which runs after
// the resolver has UPDATEd rows underneath both FTS indexes.
func (d *DB) PopulatePropertiesFTS5() error {
	return populatePropertiesFTS5(d.db)
}

// PropertiesFTS5RowCount returns the number of documents the inverted index
// actually holds, or -1 when the virtual table is absent because the binary
// was built without the `sqlite_fts5` tag.
//
// It reads `properties_fts_docsize`, the shadow table FTS5 keeps with one row
// per indexed document, and NOT `SELECT COUNT(*) FROM properties_fts`. That
// distinction is the whole point of this function. On an external-content
// table a query with no MATCH is answered from the *content* table, so
// `COUNT(*) FROM properties_fts` returns `COUNT(*) FROM properties` whether
// the index holds every document or none of them. Measured on SQLite 3.42:
// three property rows and an index that had never been built still counted 3,
// while MATCH returned nothing. The same trap is already documented against
// `nodes_fts` in main.go — "COUNT looks full, a real MATCH returns 0" — and a
// coverage gate built on that count would assert nothing at all.
//
// -1 means the capability is missing and a GT_REQUIRE_FTS5 run must abort. A
// count below `PropertyCount()` means the index exists but does not cover the
// facts, which is the failure that looks healthy from the outside.
func (d *DB) PropertiesFTS5RowCount() int {
	if !propertiesFTSExists(d.db) {
		return -1
	}
	var n int
	if err := d.db.QueryRow(`SELECT COUNT(*) FROM properties_fts_docsize`).Scan(&n); err != nil {
		return -1
	}
	return n
}
