package store

// cfg.go — persistence for the per-function statement-level control-flow
// sidecar (HAR-90 items 5–8). Three additive tables, created by createSchema
// on every open so pre-existing graph.db files gain them in place:
//
//	cfg_blocks(node_id, block_index, kind, start_line, end_line, statement_lines)
//	cfg_edges (node_id, from_block, to_block, label)
//	cfg_defs  (node_id, block_index, var_name, line)
//
// The rows are the RAW CFG the Go parser extracted — the Python consumer
// (src/groundtruth/runtime/cfg_analysis.py) composes dominators, control
// dependence, and reaching-definition chains on top. No existing table or
// column is altered: consumers that predate this sidecar read a byte-exact
// schema for everything they already know.

import (
	"database/sql"
	"fmt"
)

// CFGBlock is one row of cfg_blocks: a basic block of one function's CFG.
// StatementLines is the JSON array of 1-based statement start lines anchored
// to the block (e.g. `[12,13]`); an empty list serializes as `[]`.
type CFGBlock struct {
	NodeID         int64
	BlockIndex     int
	Kind           string
	StartLine      int
	EndLine        int
	StatementLines string
}

// CFGEdge is one row of cfg_edges: a directed edge between two block indexes
// of the same function CFG, labeled per the closed vocabulary documented in
// internal/parser/cfg.go.
type CFGEdge struct {
	NodeID    int64
	FromBlock int
	ToBlock   int
	Label     string
}

// CFGDef is one row of cfg_defs: a variable definition anchored to a block —
// assignments, declarations, updates, and loop/catch bindings. No PHI nodes.
type CFGDef struct {
	NodeID     int64
	BlockIndex int
	VarName    string
	Line       int
}

// CFGUse is one row of cfg_uses: a parser-exact identifier read anchored to
// a block — the read complement of CFGDef (v15.3).
type CFGUse struct {
	NodeID     int64
	BlockIndex int
	VarName    string
	Line       int
}

// ReplaceCFG rewrites the cfg_* sidecar wholesale in one transaction. Used by
// the full-index path (fresh staged DB, or an amend build whose retained
// nodes re-emit identical rows). Incremental reparses instead delete the
// file's rows inside DeleteFileEdgesAndNodesTx and insert via InsertCFGTx.
func (d *DB) ReplaceCFG(blocks []*CFGBlock, edges []*CFGEdge, defs []*CFGDef, uses []*CFGUse) error {
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("cfg replace begin: %w", err)
	}
	for _, stmt := range []string{
		`DELETE FROM cfg_uses`,
		`DELETE FROM cfg_defs`,
		`DELETE FROM cfg_edges`,
		`DELETE FROM cfg_blocks`,
	} {
		if _, err := tx.Exec(stmt); err != nil {
			tx.Rollback()
			return fmt.Errorf("cfg replace clear: %w", err)
		}
	}
	if err := InsertCFGTx(tx, blocks, edges, defs, uses); err != nil {
		tx.Rollback()
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("cfg replace commit: %w", err)
	}
	return nil
}

// InsertCFGTx inserts cfg rows inside the caller's transaction. Caller owns
// commit — the incremental path runs this alongside node re-insertion so the
// reparsed file's blocks/edges/defs swap atomically.
func InsertCFGTx(tx *sql.Tx, blocks []*CFGBlock, edges []*CFGEdge, defs []*CFGDef, uses []*CFGUse) error {
	if len(blocks) > 0 {
		stmt, err := tx.Prepare(
			`INSERT INTO cfg_blocks (node_id, block_index, kind, start_line, end_line, statement_lines)
			 VALUES (?, ?, ?, ?, ?, ?)`)
		if err != nil {
			return fmt.Errorf("cfg_blocks prepare: %w", err)
		}
		for _, b := range blocks {
			lines := b.StatementLines
			if lines == "" || lines == "null" {
				// json.Marshal(nil []int) is "null"; store the honest empty array
				lines = "[]"
			}
			if _, err := stmt.Exec(b.NodeID, b.BlockIndex, b.Kind,
				nullableLine(b.StartLine), nullableLine(b.EndLine), lines); err != nil {
				stmt.Close()
				return fmt.Errorf("cfg_blocks insert: %w", err)
			}
		}
		stmt.Close()
	}
	if len(edges) > 0 {
		stmt, err := tx.Prepare(
			`INSERT INTO cfg_edges (node_id, from_block, to_block, label) VALUES (?, ?, ?, ?)`)
		if err != nil {
			return fmt.Errorf("cfg_edges prepare: %w", err)
		}
		for _, e := range edges {
			if _, err := stmt.Exec(e.NodeID, e.FromBlock, e.ToBlock, e.Label); err != nil {
				stmt.Close()
				return fmt.Errorf("cfg_edges insert: %w", err)
			}
		}
		stmt.Close()
	}
	if len(defs) > 0 {
		stmt, err := tx.Prepare(
			`INSERT INTO cfg_defs (node_id, block_index, var_name, line) VALUES (?, ?, ?, ?)`)
		if err != nil {
			return fmt.Errorf("cfg_defs prepare: %w", err)
		}
		for _, df := range defs {
			if _, err := stmt.Exec(df.NodeID, df.BlockIndex, df.VarName, nullableLine(df.Line)); err != nil {
				stmt.Close()
				return fmt.Errorf("cfg_defs insert: %w", err)
			}
		}
		stmt.Close()
	}
	if len(uses) > 0 {
		stmt, err := tx.Prepare(
			`INSERT INTO cfg_uses (node_id, block_index, var_name, line) VALUES (?, ?, ?, ?)`)
		if err != nil {
			return fmt.Errorf("cfg_uses prepare: %w", err)
		}
		for _, u := range uses {
			if _, err := stmt.Exec(u.NodeID, u.BlockIndex, u.VarName, nullableLine(u.Line)); err != nil {
				stmt.Close()
				return fmt.Errorf("cfg_uses insert: %w", err)
			}
		}
		stmt.Close()
	}
	return nil
}

// nullableLine binds NULL for a line number of 0 — entry/exit-style blocks
// with no anchored statements read as "absent" rather than a bogus line 0.
func nullableLine(n int) any {
	if n <= 0 {
		return nil
	}
	return n
}
