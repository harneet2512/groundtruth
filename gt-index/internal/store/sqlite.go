// Package store handles SQLite graph database operations.
package store

import (
	"database/sql"
	"fmt"
	"log"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

// DB wraps an SQLite database for the code graph.
type DB struct {
	db *sql.DB
}

// Node represents a code entity (function, class, method, etc.)
type Node struct {
	ID            int64
	Label         string // Function, Class, Method, File, Interface, Struct, Enum, Type
	Name          string
	QualifiedName string
	FilePath      string
	StartLine     int
	EndLine       int
	Signature     string
	ReturnType    string
	IsExported    bool
	IsTest        bool
	Language      string
	ParentID      int64
}

// Edge represents a relationship between nodes.
type Edge struct {
	ID               int64
	SourceID         int64
	TargetID         int64
	Type             string // CALLS, IMPORTS, DEFINES, INHERITS, IMPLEMENTS
	SourceLine       int
	SourceFile       string
	ResolutionMethod string // same_file, import, name_match
	Confidence       float64
	Metadata         string
}

// Property represents a structural fact about a code node (guard clause, return shape, etc.)
type Property struct {
	ID         int64
	NodeID     int64
	Kind       string // guard_clause, return_shape, exception_type, raise_type, framework_call, docstring
	Value      string
	Line       int
	Confidence float64
}

// Assertion represents an assertion extracted from a test function.
type Assertion struct {
	ID           int64
	TestNodeID   int64
	TargetNodeID int64 // 0 if unresolved
	Kind         string // assertEqual, assertRaises, expect, assert, assert_eq, etc.
	Expression   string // readable assertion expression
	Expected     string // expected value if extractable
	Line         int
}

// CallSite represents an unresolved call site for LSP resolution.
type CallSite struct {
	CallerNodeID int64
	CalleeName   string
	Line         int
	Col          int
	FilePath     string
}

// Open creates or opens an SQLite graph database.
func Open(path string) (*DB, error) {
	db, err := sql.Open("sqlite3", path+"?_journal_mode=WAL&_synchronous=OFF&_busy_timeout=5000")
	if err != nil {
		return nil, fmt.Errorf("open db: %w", err)
	}
	if err := createSchema(db); err != nil {
		db.Close()
		return nil, fmt.Errorf("create schema: %w", err)
	}
	return &DB{db: db}, nil
}

// Close closes the database.
func (d *DB) Close() error { return d.db.Close() }

func createSchema(db *sql.DB) error {
	schema := `
	CREATE TABLE IF NOT EXISTS nodes (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		label TEXT NOT NULL,
		name TEXT NOT NULL,
		qualified_name TEXT,
		file_path TEXT NOT NULL,
		start_line INTEGER,
		end_line INTEGER,
		signature TEXT,
		return_type TEXT,
		is_exported BOOLEAN DEFAULT 0,
		is_test BOOLEAN DEFAULT 0,
		language TEXT NOT NULL,
		parent_id INTEGER REFERENCES nodes(id)
	);

	CREATE TABLE IF NOT EXISTS edges (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		source_id INTEGER NOT NULL REFERENCES nodes(id),
		target_id INTEGER NOT NULL REFERENCES nodes(id),
		type TEXT NOT NULL,
		source_line INTEGER,
		source_file TEXT,
		resolution_method TEXT,
		confidence REAL DEFAULT 0.0,
		metadata TEXT
	);

	CREATE TABLE IF NOT EXISTS file_hashes (
		file_path TEXT PRIMARY KEY,
		content_hash TEXT NOT NULL,
		language TEXT,
		indexed_at TEXT NOT NULL
	);

	CREATE TABLE IF NOT EXISTS project_meta (
		key TEXT PRIMARY KEY,
		value TEXT
	);

	CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
	CREATE INDEX IF NOT EXISTS idx_nodes_qname ON nodes(qualified_name);
	CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
	CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label);
	CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
	CREATE INDEX IF NOT EXISTS idx_nodes_test ON nodes(is_test) WHERE is_test = 1;
	CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
	CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
	CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
	CREATE INDEX IF NOT EXISTS idx_edges_source_type ON edges(source_id, type);
	CREATE INDEX IF NOT EXISTS idx_edges_target_type ON edges(target_id, type);
	CREATE INDEX IF NOT EXISTS idx_edges_resolution ON edges(resolution_method);
	CREATE INDEX IF NOT EXISTS idx_edges_confidence ON edges(confidence);

	CREATE TABLE IF NOT EXISTS properties (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		node_id INTEGER NOT NULL REFERENCES nodes(id),
		kind TEXT NOT NULL,
		value TEXT NOT NULL,
		line INTEGER,
		confidence REAL DEFAULT 1.0
	);

	CREATE TABLE IF NOT EXISTS assertions (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		test_node_id INTEGER NOT NULL REFERENCES nodes(id),
		target_node_id INTEGER DEFAULT 0,
		kind TEXT NOT NULL,
		expression TEXT NOT NULL,
		expected TEXT,
		line INTEGER
	);

	CREATE TABLE IF NOT EXISTS call_sites (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		caller_node_id INTEGER NOT NULL REFERENCES nodes(id),
		callee_name TEXT NOT NULL,
		line INTEGER NOT NULL,
		col INTEGER NOT NULL,
		file_path TEXT NOT NULL,
		resolved BOOLEAN DEFAULT 0
	);

	CREATE INDEX IF NOT EXISTS idx_properties_node ON properties(node_id);
	CREATE INDEX IF NOT EXISTS idx_properties_kind ON properties(kind);
	CREATE INDEX IF NOT EXISTS idx_properties_node_kind ON properties(node_id, kind);
	CREATE INDEX IF NOT EXISTS idx_assertions_test ON assertions(test_node_id);
	CREATE INDEX IF NOT EXISTS idx_assertions_target ON assertions(target_node_id);
	CREATE INDEX IF NOT EXISTS idx_call_sites_file ON call_sites(file_path);
	CREATE INDEX IF NOT EXISTS idx_call_sites_resolved ON call_sites(resolved);
	CREATE INDEX IF NOT EXISTS idx_call_sites_caller ON call_sites(caller_node_id);
	`
	_, err := db.Exec(schema)
	return err
}

// InsertNode inserts a node and returns its ID.
func (d *DB) InsertNode(n *Node) (int64, error) {
	res, err := d.db.Exec(
		`INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line,
		 signature, return_type, is_exported, is_test, language, parent_id)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		n.Label, n.Name, n.QualifiedName, n.FilePath, n.StartLine, n.EndLine,
		n.Signature, n.ReturnType, n.IsExported, n.IsTest, n.Language, n.ParentID,
	)
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

// InsertEdge inserts an edge.
func (d *DB) InsertEdge(e *Edge) error {
	_, err := d.db.Exec(
		`INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence, metadata)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		e.SourceID, e.TargetID, e.Type, e.SourceLine, e.SourceFile, e.ResolutionMethod, e.Confidence, e.Metadata,
	)
	return err
}

// InsertFileHash records a file's content hash for incremental reindexing.
func (d *DB) InsertFileHash(filePath, hash, language string) error {
	_, err := d.db.Exec(
		`INSERT OR REPLACE INTO file_hashes (file_path, content_hash, language, indexed_at) VALUES (?, ?, ?, ?)`,
		filePath, hash, language, time.Now().UTC().Format(time.RFC3339),
	)
	return err
}

// SetMeta stores a key-value pair in project_meta.
func (d *DB) SetMeta(key, value string) error {
	_, err := d.db.Exec(`INSERT OR REPLACE INTO project_meta (key, value) VALUES (?, ?)`, key, value)
	return err
}

// GetFileHash returns the stored hash for a file, or empty string if not found.
func (d *DB) GetFileHash(filePath string) string {
	var hash string
	d.db.QueryRow(`SELECT content_hash FROM file_hashes WHERE file_path = ?`, filePath).Scan(&hash)
	return hash
}

// BeginTx starts a transaction for batch inserts.
func (d *DB) BeginTx() (*sql.Tx, error) { return d.db.Begin() }

// BatchInsertNodes inserts nodes in a single transaction with a prepared statement.
// Returns the auto-generated IDs in the same order as input.
func (d *DB) BatchInsertNodes(nodes []*Node) ([]int64, error) {
	tx, err := d.db.Begin()
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	stmt, err := tx.Prepare(
		`INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line,
		 signature, return_type, is_exported, is_test, language, parent_id)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		tx.Rollback()
		return nil, fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	ids := make([]int64, len(nodes))
	for i, n := range nodes {
		res, err := stmt.Exec(
			n.Label, n.Name, n.QualifiedName, n.FilePath, n.StartLine, n.EndLine,
			n.Signature, n.ReturnType, n.IsExported, n.IsTest, n.Language, n.ParentID,
		)
		if err != nil {
			tx.Rollback()
			return nil, fmt.Errorf("insert node %d: %w", i, err)
		}
		id, err := res.LastInsertId()
		if err != nil {
			log.Printf("WARNING: LastInsertId failed for node %d: %v", i, err)
			continue
		}
		ids[i] = id
	}
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("commit: %w", err)
	}
	return ids, nil
}

// BatchInsertEdges inserts edges in a single transaction with a prepared statement.
func (d *DB) BatchInsertEdges(edges []*Edge) error {
	if len(edges) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	stmt, err := tx.Prepare(
		`INSERT INTO edges (source_id, target_id, type, source_line, source_file,
		 resolution_method, confidence, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	for i, e := range edges {
		_, err := stmt.Exec(
			e.SourceID, e.TargetID, e.Type, e.SourceLine, e.SourceFile,
			e.ResolutionMethod, e.Confidence, e.Metadata,
		)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("insert edge %d: %w", i, err)
		}
	}
	return tx.Commit()
}

// LookupNodeByName finds nodes by name. Returns slice of node IDs.
func (d *DB) LookupNodeByName(name string) []int64 {
	rows, err := d.db.Query(`SELECT id FROM nodes WHERE name = ?`, name)
	if err != nil {
		return nil
	}
	defer rows.Close()
	var ids []int64
	for rows.Next() {
		var id int64
		rows.Scan(&id)
		ids = append(ids, id)
	}
	return ids
}

// UpdateParentID sets the parent_id for a node after batch insert.
func (d *DB) UpdateParentID(nodeID, parentID int64) {
	d.db.Exec("UPDATE nodes SET parent_id = ? WHERE id = ?", parentID, nodeID)
}

// NodeCount returns total number of nodes.
func (d *DB) NodeCount() int {
	var count int
	d.db.QueryRow(`SELECT COUNT(*) FROM nodes`).Scan(&count)
	return count
}

// EdgeCount returns total number of edges.
func (d *DB) EdgeCount() int {
	var count int
	d.db.QueryRow(`SELECT COUNT(*) FROM edges`).Scan(&count)
	return count
}

// PropertyCount returns total number of properties.
func (d *DB) PropertyCount() int {
	var count int
	d.db.QueryRow(`SELECT COUNT(*) FROM properties`).Scan(&count)
	return count
}

// AssertionCount returns total number of assertions.
func (d *DB) AssertionCount() int {
	var count int
	d.db.QueryRow(`SELECT COUNT(*) FROM assertions`).Scan(&count)
	return count
}

// InsertProperty inserts a property for a node.
func (d *DB) InsertProperty(p *Property) error {
	_, err := d.db.Exec(
		`INSERT INTO properties (node_id, kind, value, line, confidence) VALUES (?, ?, ?, ?, ?)`,
		p.NodeID, p.Kind, p.Value, p.Line, p.Confidence,
	)
	return err
}

// InsertAssertion inserts an assertion from a test function.
func (d *DB) InsertAssertion(a *Assertion) error {
	_, err := d.db.Exec(
		`INSERT INTO assertions (test_node_id, target_node_id, kind, expression, expected, line) VALUES (?, ?, ?, ?, ?, ?)`,
		a.TestNodeID, a.TargetNodeID, a.Kind, a.Expression, a.Expected, a.Line,
	)
	return err
}

// BatchInsertProperties inserts properties in a single transaction.
func (d *DB) BatchInsertProperties(props []*Property) error {
	if len(props) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	stmt, err := tx.Prepare(
		`INSERT INTO properties (node_id, kind, value, line, confidence) VALUES (?, ?, ?, ?, ?)`,
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	for i, p := range props {
		_, err := stmt.Exec(p.NodeID, p.Kind, p.Value, p.Line, p.Confidence)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("insert property %d: %w", i, err)
		}
	}
	return tx.Commit()
}

// BatchInsertAssertions inserts assertions in a single transaction.
func (d *DB) BatchInsertAssertions(assertions []*Assertion) error {
	if len(assertions) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	stmt, err := tx.Prepare(
		`INSERT INTO assertions (test_node_id, target_node_id, kind, expression, expected, line) VALUES (?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	for i, a := range assertions {
		_, err := stmt.Exec(a.TestNodeID, a.TargetNodeID, a.Kind, a.Expression, a.Expected, a.Line)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("insert assertion %d: %w", i, err)
		}
	}
	return tx.Commit()
}

// BatchInsertCallSites inserts unresolved call sites in a single transaction.
func (d *DB) BatchInsertCallSites(sites []*CallSite) error {
	if len(sites) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	stmt, err := tx.Prepare(
		`INSERT INTO call_sites (caller_node_id, callee_name, line, col, file_path, resolved) VALUES (?, ?, ?, ?, ?, 0)`,
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	for i, s := range sites {
		_, err := stmt.Exec(s.CallerNodeID, s.CalleeName, s.Line, s.Col, s.FilePath)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("insert call_site %d: %w", i, err)
		}
	}
	return tx.Commit()
}

// CallSiteCount returns total number of call sites.
func (d *DB) CallSiteCount() int {
	var count int
	d.db.QueryRow(`SELECT COUNT(*) FROM call_sites`).Scan(&count)
	return count
}
