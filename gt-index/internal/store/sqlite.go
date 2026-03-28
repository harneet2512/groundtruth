// Package store handles SQLite graph database operations.
package store

import (
	"database/sql"
	"fmt"
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
	ResolutionMethod string // import, fqn, aho-corasick
	Metadata         string
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
		`INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, metadata)
		 VALUES (?, ?, ?, ?, ?, ?, ?)`,
		e.SourceID, e.TargetID, e.Type, e.SourceLine, e.SourceFile, e.ResolutionMethod, e.Metadata,
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
