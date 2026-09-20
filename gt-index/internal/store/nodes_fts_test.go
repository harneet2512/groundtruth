package store

import (
	"math"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// nodes_fts is the external-content FTS5 index `lexical_rank` scores with
// bm25(). A paid DeepSWE run (gate-one, run 35056493769) shipped a graph whose
// index carried a dead node generation: nodes_fts_docsize held 190,953 rows
// while nodes held 95,644, so bm25's idf term evaluated log() of a
// non-positive value, SQLite mapped the NaN to NULL, and the consumer crashed
// on float(None). These tests pin the maintenance contract that keeps the
// index integral: rebuild-from-content, recovery on malformed shadow tables,
// and a post-populate verification that would have caught that graph before
// it was published.

// nodesFixture builds a small graph whose nodes carry searchable text.
func nodesFixture(t *testing.T, names []string) *DB {
	t.Helper()
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	for i, name := range names {
		if _, err := db.db.Exec(
			`INSERT INTO nodes (id, label, name, qualified_name, file_path, language)
			 VALUES (?, 'Function', ?, ?, ?, 'python')`,
			i+1, name, "mod."+name, "src/"+name+".py",
		); err != nil {
			t.Fatal(err)
		}
	}
	return db
}

// corruptAveragesRecord forces the production inconsistency: the averages
// record claims claimedRows indexed rows while the doclists hold more. On
// SQLite builds whose bm25 reads the stats varint (the consumer's Python
// driver), the idf term then goes log(<=0) -> NaN -> NULL on any term whose
// doclist is longer than the claim. On builds that read doclist cardinality
// directly the score stays finite — but the record is still stale, and
// rebuild must rewrite it either way.
func corruptAveragesRecord(t *testing.T, db *DB, claimedRows int) {
	t.Helper()
	var block []byte
	if err := db.db.QueryRow(`SELECT block FROM nodes_fts_data WHERE id=1`).Scan(&block); err != nil {
		t.Fatalf("nodes_fts_data has no averages record: %v", err)
	}
	if len(block) < 1 {
		t.Fatal("averages record is empty")
	}
	block[0] = byte(claimedRows)
	if _, err := db.db.Exec(`UPDATE nodes_fts_data SET block=? WHERE id=1`, block); err != nil {
		t.Fatalf("corrupt averages record: %v", err)
	}
}

// averagesRowCount reads back the nRow varint at the head of the id=1
// averages record — small fixture counts keep it a single byte.
func averagesRowCount(t *testing.T, db *DB) int {
	t.Helper()
	var block []byte
	if err := db.db.QueryRow(`SELECT block FROM nodes_fts_data WHERE id=1`).Scan(&block); err != nil {
		t.Fatalf("read averages record: %v", err)
	}
	if len(block) < 1 {
		t.Fatal("averages record is empty")
	}
	return int(block[0])
}

// bm25Scores returns the raw bm25() value for every hit on term, so a test can
// assert finiteness — NULL surfaces as a nil *float64.
func bm25Scores(t *testing.T, db *DB, term string) []*float64 {
	t.Helper()
	rows, err := db.db.Query(
		`SELECT bm25(nodes_fts) FROM nodes_fts WHERE nodes_fts MATCH ?`, term)
	if err != nil {
		t.Fatalf("MATCH %q: %v", term, err)
	}
	defer rows.Close()
	var scores []*float64
	for rows.Next() {
		var s *float64
		if err := rows.Scan(&s); err != nil {
			t.Fatal(err)
		}
		scores = append(scores, s)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return scores
}

func assertAllScoresFinite(t *testing.T, scores []*float64) {
	t.Helper()
	if len(scores) == 0 {
		t.Fatal("expected at least one index hit")
	}
	for i, s := range scores {
		if s == nil {
			t.Fatalf("bm25 hit %d is NULL — the index statistics are inconsistent with its doclists", i)
		}
		if math.IsNaN(*s) || math.IsInf(*s, 0) {
			t.Fatalf("bm25 hit %d is %v", i, *s)
		}
	}
}

// TestPopulateFTS5LeavesAQueryableIndex: after population every matched row
// scores finite and the index covers exactly the content rows.
func TestPopulateFTS5LeavesAQueryableIndex(t *testing.T) {
	db := nodesFixture(t, []string{"alpha", "beta", "alphabet", "gamma"})
	if err := db.PopulateFTS5(); err != nil {
		t.Fatal(err)
	}
	assertAllScoresFinite(t, bm25Scores(t, db, "alpha"))
	if n := scalarInt(t, db.db, `SELECT COUNT(*) FROM nodes_fts_docsize`); n != 4 {
		t.Fatalf("nodes_fts_docsize=%d, want one row per node (4)", n)
	}
}

// TestPopulateFTS5RepairsAStaleStatsRecord: the averages record claims fewer
// rows than the index holds — the stats half of the gate-one divergence.
// bm25 on this build reads doclist cardinality so the stale record is not
// NULL-visible here; what IS observable is that rebuild regenerates the
// record from content, restoring the true row count.
func TestPopulateFTS5RepairsAStaleStatsRecord(t *testing.T) {
	db := nodesFixture(t, []string{"alpha", "beta", "alphabet", "gamma"})
	if err := db.PopulateFTS5(); err != nil {
		t.Fatal(err)
	}
	corruptAveragesRecord(t, db, 1)
	if n := averagesRowCount(t, db); n != 1 {
		t.Fatalf("premise broken: averages record claims %d rows, want 1", n)
	}
	if err := db.PopulateFTS5(); err != nil {
		t.Fatalf("PopulateFTS5 must repair the stale stats record: %v", err)
	}
	if n := averagesRowCount(t, db); n != 4 {
		t.Fatalf("after rebuild the averages record still claims %d rows, want 4 — 'rebuild' must regenerate it from content", n)
	}
	assertAllScoresFinite(t, bm25Scores(t, db, "alpha"))
	if n := scalarInt(t, db.db, `SELECT COUNT(*) FROM nodes_fts_docsize`); n != 4 {
		t.Fatalf("after repair nodes_fts_docsize=%d, want 4", n)
	}
}

// TestPopulateFTS5DropsTheDeadGeneration: a node-generation swap deletes every
// content row and reinserts under fresh rowids — the amend shape that produced
// the corrupt graph. Maintenance must leave the index holding exactly the live
// generation: no docsize row may name a dead rowid, and the count invariant
// docsize == nodes must hold.
func TestPopulateFTS5DropsTheDeadGeneration(t *testing.T) {
	db := nodesFixture(t, []string{"alpha", "beta", "alphabet", "gamma"})
	if err := db.PopulateFTS5(); err != nil {
		t.Fatal(err)
	}
	// Generation swap: delete all content, reinsert under new rowids.
	if _, err := db.db.Exec(`DELETE FROM nodes`); err != nil {
		t.Fatal(err)
	}
	for i, name := range []string{"delta", "epsilon", "zeta"} {
		if _, err := db.db.Exec(
			`INSERT INTO nodes (id, label, name, qualified_name, file_path, language)
			 VALUES (?, 'Function', ?, ?, ?, 'python')`,
			100+i, name, "mod."+name, "src/"+name+".py",
		); err != nil {
			t.Fatal(err)
		}
	}
	if err := db.PopulateFTS5(); err != nil {
		t.Fatal(err)
	}
	if dead := scalarInt(t, db.db,
		`SELECT COUNT(*) FROM nodes_fts_docsize WHERE id NOT IN (SELECT id FROM nodes)`); dead != 0 {
		t.Fatalf("%d docsize rows name dead rowids — a dead generation survived maintenance", dead)
	}
	if n := scalarInt(t, db.db, `SELECT COUNT(*) FROM nodes_fts_docsize`); n != 3 {
		t.Fatalf("nodes_fts_docsize=%d, want 3 (the live generation)", n)
	}
	assertAllScoresFinite(t, bm25Scores(t, db, "delta"))
}

// TestPopulateFTS5RepairsAMalformedIndex: wiping a shadow table leaves an
// index whose MATCH fails with "database disk image is malformed". PopulateFTS5
// must return the index to health — by rebuild where that suffices, by
// DROP+recreate where the damage is structural.
func TestPopulateFTS5RepairsAMalformedIndex(t *testing.T) {
	db := nodesFixture(t, []string{"alpha", "beta", "alphabet", "gamma"})
	if err := db.PopulateFTS5(); err != nil {
		t.Fatal(err)
	}
	if _, err := db.db.Exec(`DELETE FROM nodes_fts_docsize`); err != nil {
		t.Fatal(err)
	}
	var probe *float64
	if err := db.db.QueryRow(
		`SELECT bm25(nodes_fts) FROM nodes_fts WHERE nodes_fts MATCH 'alpha' LIMIT 1`,
	).Scan(&probe); err == nil {
		t.Fatal("premise broken: MATCH on a docsize-less index should fail as malformed")
	}
	if err := db.PopulateFTS5(); err != nil {
		t.Fatalf("PopulateFTS5 must self-heal a malformed index: %v", err)
	}
	assertAllScoresFinite(t, bm25Scores(t, db, "alpha"))
	if n := scalarInt(t, db.db, `SELECT COUNT(*) FROM nodes_fts_docsize`); n != 4 {
		t.Fatalf("after recovery nodes_fts_docsize=%d, want 4", n)
	}
}

// TestFTS5IndexCountReadsTheIndexNotTheContent: COUNT(*) FROM nodes_fts on an
// external-content table is answered from `nodes`, so it cannot see an empty
// or divergent index. The integrity measure is nodes_fts_docsize parity with
// nodes — the invariant the corrupt graph violated.
func TestFTS5IndexCountReadsTheIndexNotTheContent(t *testing.T) {
	db := nodesFixture(t, []string{"alpha", "beta", "alphabet", "gamma"})
	if err := db.PopulateFTS5(); err != nil {
		t.Fatal(err)
	}
	if n := db.FTS5RowCount(); n != 4 {
		t.Fatalf("FTS5RowCount=%d on a healthy index, want 4", n)
	}
	// Empty the index behind the content table's back.
	if _, err := db.db.Exec(`INSERT INTO nodes_fts(nodes_fts) VALUES('delete-all')`); err != nil {
		t.Fatal(err)
	}
	if n := scalarInt(t, db.db, `SELECT COUNT(*) FROM nodes_fts`); n != 4 {
		t.Fatalf("premise broken: COUNT(*) FROM nodes_fts=%d still reads content", n)
	}
	if n := db.FTS5RowCount(); n != 0 {
		t.Fatalf("FTS5RowCount=%d on an emptied index — it is reading the content table, not the index", n)
	}
}

// TestVerifyFTS5IntegrityFailsOnDesyncedIndex: the post-populate verification
// is what turns "WARN and publish" into "refuse to publish". A docsize row
// whose rowid is dead is the dead-generation signature the paid graph shipped
// (190,953 index docs vs 95,644 nodes) — driver-independent, because it is a
// count invariant, not a scoring behaviour.
func TestVerifyFTS5IntegrityFailsOnDesyncedIndex(t *testing.T) {
	db := nodesFixture(t, []string{"alpha", "beta", "alphabet", "gamma"})
	if err := db.PopulateFTS5(); err != nil {
		t.Fatal(err)
	}
	if err := db.VerifyFTS5Integrity(); err != nil {
		t.Fatalf("verify must pass on a healthy index: %v", err)
	}
	if _, err := db.db.Exec(`INSERT INTO nodes_fts_docsize(id, sz) VALUES(9999, 1)`); err != nil {
		t.Fatal(err)
	}
	if err := db.VerifyFTS5Integrity(); err == nil {
		t.Fatal("verify must fail on a desynced index — docsize 5 vs nodes 4 is the dead-generation signature")
	}
	// And PopulateFTS5's rebuild clears the dead row, after which verify passes.
	if err := db.PopulateFTS5(); err != nil {
		t.Fatalf("PopulateFTS5 must clear the dead docsize row: %v", err)
	}
	if n := scalarInt(t, db.db, `SELECT COUNT(*) FROM nodes_fts_docsize`); n != 4 {
		t.Fatalf("after repair nodes_fts_docsize=%d, want 4", n)
	}
}
