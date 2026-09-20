package store

// cfg_test.go — store-side coverage for the cfg_* sidecar (HAR-90 items 5–8):
// round-trip persistence through ReplaceCFG, and per-file retirement inside
// DeleteFileEdgesAndNodesTx + re-emission via InsertCFGTx (the incremental
// reparse contract: a reparsed file's stale CFG rows must never survive).

import (
	"database/sql"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

func cfgTestDB(t *testing.T) *sql.DB {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := createSchema(db); err != nil {
		t.Fatal(err)
	}
	return db
}

func countRows(t *testing.T, db *sql.DB, q string, args ...any) int {
	t.Helper()
	var n int
	if err := db.QueryRow(q, args...).Scan(&n); err != nil {
		t.Fatalf("count %q: %v", q, err)
	}
	return n
}

func TestCFGRoundTrip(t *testing.T) {
	db := cfgTestDB(t)
	if _, err := db.Exec(
		`INSERT INTO nodes (id, label, name, file_path, language) VALUES
		 (1, 'Function', 'flowExercise', 'web.ts', 'typescript')`,
	); err != nil {
		t.Fatal(err)
	}

	blocks := []*CFGBlock{
		{NodeID: 1, BlockIndex: 0, Kind: "entry", StartLine: 44, EndLine: 44, StatementLines: "[44]"},
		{NodeID: 1, BlockIndex: 1, Kind: "exit", StatementLines: "[]"},
		{NodeID: 1, BlockIndex: 2, Kind: "block", StartLine: 45, EndLine: 46, StatementLines: "[45,46]"},
		{NodeID: 1, BlockIndex: 3, Kind: "if_then", StartLine: 47, EndLine: 47, StatementLines: "[47]"},
		{NodeID: 1, BlockIndex: 4, Kind: "if_else", StartLine: 49, EndLine: 49, StatementLines: "[49]"},
	}
	edges := []*CFGEdge{
		{NodeID: 1, FromBlock: 0, ToBlock: 2, Label: "entry"},
		{NodeID: 1, FromBlock: 2, ToBlock: 3, Label: "true"},
		{NodeID: 1, FromBlock: 2, ToBlock: 4, Label: "false"},
		{NodeID: 1, FromBlock: 3, ToBlock: 5, Label: ""},
		{NodeID: 1, FromBlock: 4, ToBlock: 5, Label: ""},
		{NodeID: 1, FromBlock: 5, ToBlock: 1, Label: "end"},
	}
	defs := []*CFGDef{
		{NodeID: 1, BlockIndex: 0, VarName: "n", Line: 44},
		{NodeID: 1, BlockIndex: 2, VarName: "total", Line: 45},
		{NodeID: 1, BlockIndex: 3, VarName: "total", Line: 47},
	}
	d := &DB{db: db}
	uses := []*CFGUse{
		{NodeID: 1, BlockIndex: 0, VarName: "n", Line: 44},
		{NodeID: 1, BlockIndex: 2, VarName: "items", Line: 46},
		{NodeID: 1, BlockIndex: 3, VarName: "total", Line: 47},
	}
	if err := d.ReplaceCFG(blocks, edges, defs, uses); err != nil {
		t.Fatalf("ReplaceCFG: %v", err)
	}

	if n := countRows(t, db, `SELECT count(*) FROM cfg_blocks WHERE node_id=1`); n != 5 {
		t.Fatalf("cfg_blocks rows=%d want 5", n)
	}
	if n := countRows(t, db, `SELECT count(*) FROM cfg_edges WHERE node_id=1`); n != 6 {
		t.Fatalf("cfg_edges rows=%d want 6", n)
	}
	if n := countRows(t, db, `SELECT count(*) FROM cfg_defs WHERE node_id=1`); n != 3 {
		t.Fatalf("cfg_defs rows=%d want 3", n)
	}
	if n := countRows(t, db, `SELECT count(*) FROM cfg_uses WHERE node_id=1`); n != 3 {
		t.Fatalf("cfg_uses rows=%d want 3", n)
	}

	// verify column fidelity on one block and one edge
	var kind, stmtLines string
	var start, end sql.NullInt64
	if err := db.QueryRow(
		`SELECT kind, start_line, end_line, statement_lines FROM cfg_blocks WHERE node_id=1 AND block_index=3`,
	).Scan(&kind, &start, &end, &stmtLines); err != nil {
		t.Fatal(err)
	}
	if kind != "if_then" || stmtLines != "[47]" || !start.Valid || start.Int64 != 47 {
		t.Fatalf("block row mismatch: %s %v %v %s", kind, start, end, stmtLines)
	}
	// exit block: no statements -> NULL lines, '[]'
	if err := db.QueryRow(
		`SELECT start_line, end_line, statement_lines FROM cfg_blocks WHERE node_id=1 AND block_index=1`,
	).Scan(&start, &end, &stmtLines); err != nil {
		t.Fatal(err)
	}
	if start.Valid || end.Valid || stmtLines != "[]" {
		t.Fatalf("exit block should have NULL lines/'[]', got %v %v %s", start, end, stmtLines)
	}
	var label string
	var fb, tb int
	if err := db.QueryRow(
		`SELECT from_block, to_block, label FROM cfg_edges WHERE node_id=1 AND label='false'`,
	).Scan(&fb, &tb, &label); err != nil {
		t.Fatal(err)
	}
	if fb != 2 || tb != 4 || label != "false" {
		t.Fatalf("edge row mismatch: %d -> %d %q", fb, tb, label)
	}
	var vn string
	var ln sql.NullInt64
	if err := db.QueryRow(
		`SELECT var_name, line FROM cfg_defs WHERE node_id=1 AND block_index=0`,
	).Scan(&vn, &ln); err != nil {
		t.Fatal(err)
	}
	if vn != "n" || !ln.Valid || ln.Int64 != 44 {
		t.Fatalf("def row mismatch: %s %v", vn, ln)
	}
}

func TestCFGReparseCleanup(t *testing.T) {
	db := cfgTestDB(t)
	if _, err := db.Exec(
		`INSERT INTO nodes (id, label, name, file_path, language) VALUES
		 (1, 'Function', 'f', 'a.ts', 'typescript'),
		 (2, 'Function', 'g', 'b.ts', 'typescript')`,
	); err != nil {
		t.Fatal(err)
	}
	d := &DB{db: db}
	// initial emission for both files
	if err := d.ReplaceCFG(
		[]*CFGBlock{
			{NodeID: 1, BlockIndex: 0, Kind: "entry", StartLine: 1, EndLine: 1, StatementLines: "[1]"},
			{NodeID: 1, BlockIndex: 2, Kind: "block", StartLine: 2, EndLine: 2, StatementLines: "[2]"},
			{NodeID: 2, BlockIndex: 0, Kind: "entry", StartLine: 1, EndLine: 1, StatementLines: "[1]"},
		},
		[]*CFGEdge{
			{NodeID: 1, FromBlock: 0, ToBlock: 2, Label: "entry"},
			{NodeID: 2, FromBlock: 0, ToBlock: 2, Label: "entry"},
		},
		[]*CFGDef{
			{NodeID: 1, BlockIndex: 0, VarName: "x", Line: 1},
			{NodeID: 2, BlockIndex: 0, VarName: "y", Line: 1},
		},
		[]*CFGUse{
			{NodeID: 1, BlockIndex: 0, VarName: "src", Line: 1},
			{NodeID: 2, BlockIndex: 0, VarName: "src", Line: 1},
		},
	); err != nil {
		t.Fatalf("ReplaceCFG: %v", err)
	}

	// incremental reparse of a.ts: delete file rows + nodes, insert fresh node
	// + fresh cfg rows, all inside one tx — mirroring runIncremental's order.
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := DeleteFileEdgesAndNodesTx(tx, "a.ts"); err != nil {
		tx.Rollback()
		t.Fatalf("DeleteFileEdgesAndNodesTx: %v", err)
	}
	res, err := tx.Exec(
		`INSERT INTO nodes (label, name, file_path, language) VALUES ('Function','f2','a.ts','typescript')`)
	if err != nil {
		tx.Rollback()
		t.Fatal(err)
	}
	newID, _ := res.LastInsertId()
	if err := InsertCFGTx(tx,
		[]*CFGBlock{{NodeID: newID, BlockIndex: 0, Kind: "entry", StartLine: 1, EndLine: 1, StatementLines: "[1]"}},
		[]*CFGEdge{{NodeID: newID, FromBlock: 0, ToBlock: 1, Label: "entry"}},
		[]*CFGDef{{NodeID: newID, BlockIndex: 0, VarName: "z", Line: 1}},
		[]*CFGUse{{NodeID: newID, BlockIndex: 0, VarName: "src", Line: 1}},
	); err != nil {
		tx.Rollback()
		t.Fatalf("InsertCFGTx: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}

	// a.ts: exactly the fresh rows for the new node; nothing stale survives.
	if n := countRows(t, db,
		`SELECT count(*) FROM cfg_blocks WHERE node_id IN (SELECT id FROM nodes WHERE file_path='a.ts')`); n != 1 {
		t.Fatalf("stale/missing cfg_blocks for a.ts: %d", n)
	}
	if n := countRows(t, db,
		`SELECT count(*) FROM cfg_edges WHERE node_id=?`, newID); n != 1 {
		t.Fatalf("cfg_edges for new node: %d", n)
	}
	if n := countRows(t, db,
		`SELECT count(*) FROM cfg_defs WHERE node_id=? AND var_name='z'`, newID); n != 1 {
		t.Fatalf("cfg_defs for new node: %d", n)
	}
	// cfg_uses follows the same per-file retirement: a.ts's stale use row
	// is gone, the fresh one persists, b.ts's is untouched.
	if n := countRows(t, db,
		`SELECT count(*) FROM cfg_uses WHERE node_id=1`); n != 0 {
		t.Fatalf("stale cfg_uses for a.ts: %d", n)
	}
	if n := countRows(t, db,
		`SELECT count(*) FROM cfg_uses WHERE node_id=? AND var_name='src'`, newID); n != 1 {
		t.Fatalf("cfg_uses for new node: %d", n)
	}
	// b.ts rows untouched
	if n := countRows(t, db, `SELECT count(*) FROM cfg_blocks WHERE node_id=2`); n != 1 {
		t.Fatalf("b.ts cfg_blocks clobbered: %d", n)
	}
	if n := countRows(t, db, `SELECT count(*) FROM cfg_defs WHERE node_id=2 AND var_name='y'`); n != 1 {
		t.Fatalf("b.ts cfg_defs clobbered: %d", n)
	}
	// no dangling rows for deleted node id 1
	if n := countRows(t, db, `SELECT count(*) FROM cfg_blocks WHERE node_id=1`); n != 0 {
		t.Fatalf("dangling cfg_blocks for deleted node: %d", n)
	}
}

// TestCFGEmptyReplace: a reparse yielding no CFG still leaves the tables
// consistent — ReplaceCFG clears wholesale; empty input writes zero rows.
func TestCFGEmptyReplace(t *testing.T) {
	db := cfgTestDB(t)
	d := &DB{db: db}
	if err := d.ReplaceCFG(nil, nil, nil, nil); err != nil {
		t.Fatalf("ReplaceCFG empty: %v", err)
	}
	for _, table := range []string{"cfg_blocks", "cfg_edges", "cfg_defs", "cfg_uses"} {
		if n := countRows(t, db, `SELECT count(*) FROM `+table); n != 0 {
			t.Fatalf("%s not empty after empty replace: %d", table, n)
		}
	}
}
