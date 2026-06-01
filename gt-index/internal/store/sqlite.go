// Package store handles SQLite graph database operations.
package store

import (
	"database/sql"
	"fmt"
	"log"
	"os"
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
	ID                 int64
	SourceID           int64
	TargetID           int64
	Type               string // CALLS, IMPORTS, DEFINES, INHERITS, IMPLEMENTS
	SourceLine         int
	SourceFile         string
	ResolutionMethod   string // same_file, import, name_match
	Confidence         float64
	Metadata           string
	TrustTier          string // CERTIFIED, CANDIDATE, SPECULATIVE, SUPPRESSED
	CandidateCount     int
	EvidenceType       string // ast_call, ast_import, name_match
	VerificationStatus string // unverified, verified, rejected
}

// Closure is one row of the transitive-reachability sidecar (C7 / RF-4).
// SourceID transitively reaches TargetID in Depth hops; MinConfidence is the
// weakest edge confidence along that path (the path is built over VERIFIED
// edges only — see internal/closure).
type Closure struct {
	SourceID      int64
	TargetID      int64
	Depth         int
	MinConfidence float64
}

// Property represents a structural fact about a code node (guard clause, return shape, etc.)
type Property struct {
	ID         int64
	NodeID     int64
	Kind       string // guard_clause, return_shape, exception_type, docstring, caller_usage, conditional_return, side_effect, param, security_tag, exception_flow, exception_handler, fingerprint, field_read, boundary_condition, class_field, class_decorator
	Value      string
	Line       int
	Confidence float64
}

// Assertion represents an assertion extracted from a test function.
type Assertion struct {
	ID              int64
	TestNodeID      int64
	TargetNodeID    int64   // 0 if unresolved
	ResolutionScore float64 // multi-signal score that produced the link
	Kind            string  // assertEqual, assertRaises, expect, assert, assert_eq, etc.
	Expression      string  // readable assertion expression
	Expected        string  // expected value if extractable
	Line            int
}

// Open creates or opens an SQLite graph database.
//
// RC-04: synchronous=NORMAL (not OFF) is the minimum safe setting for WAL —
// guarantees durability across power loss / OOM / SIGKILL after a successful
// Commit, while still avoiding the per-transaction fsync of FULL. OFF was
// silently corrupting the WAL when the indexer was killed mid-write.
func Open(path string) (*DB, error) {
	db, err := sql.Open("sqlite3", path+"?_journal_mode=WAL&_synchronous=NORMAL&_busy_timeout=5000")
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

// ValidateForeignKeys checks all FK constraints after data is fully loaded.
// Called post-insert (not during) because batch inserts may reference
// parent nodes not yet inserted at the time of the child INSERT.
func (d *DB) ValidateForeignKeys() error {
	rows, err := d.db.Query("PRAGMA foreign_key_check")
	if err != nil {
		return fmt.Errorf("fk check: %w", err)
	}
	defer rows.Close()
	violations := 0
	for rows.Next() {
		violations++
	}
	if violations > 0 {
		log.Printf("WARNING: %d foreign key violations found in graph.db", violations)
	}
	return nil
}

// CheckpointWAL forces a TRUNCATE checkpoint so all WAL frames are folded
// into the main database file and the WAL is reset. Called after each
// transaction commit during incremental reindex; bounds reader-vs-writer
// torn-read windows and shrinks the WAL footprint on shared volumes.
//
// RC-04: failure here is non-fatal (logged) — the data has been Commit'd, the
// checkpoint is purely a hygiene step.
func (d *DB) CheckpointWAL() {
	if _, err := d.db.Exec("PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
		log.Printf("WARNING: wal_checkpoint(TRUNCATE) failed: %v", err)
	}
}

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
		metadata TEXT,
		trust_tier TEXT DEFAULT 'SPECULATIVE',
		candidate_count INTEGER DEFAULT 1,
		evidence_type TEXT,
		verification_status TEXT DEFAULT 'unverified'
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
	CREATE INDEX IF NOT EXISTS idx_edges_trust_tier ON edges(trust_tier);
	CREATE INDEX IF NOT EXISTS idx_edges_target_tier ON edges(target_id, trust_tier);
	CREATE INDEX IF NOT EXISTS idx_edges_source_file ON edges(source_file);

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
		resolution_score REAL DEFAULT 0.0,
		kind TEXT NOT NULL,
		expression TEXT NOT NULL,
		expected TEXT,
		line INTEGER
	);

	CREATE INDEX IF NOT EXISTS idx_properties_node ON properties(node_id);
	CREATE INDEX IF NOT EXISTS idx_properties_kind ON properties(kind);
	CREATE INDEX IF NOT EXISTS idx_properties_node_kind ON properties(node_id, kind);
	CREATE INDEX IF NOT EXISTS idx_assertions_test ON assertions(test_node_id);
	CREATE INDEX IF NOT EXISTS idx_assertions_target ON assertions(target_node_id);

	CREATE TABLE IF NOT EXISTS cochanges (
		file_a TEXT NOT NULL,
		file_b TEXT NOT NULL,
		count INTEGER NOT NULL DEFAULT 1,
		PRIMARY KEY(file_a, file_b)
	);
	CREATE INDEX IF NOT EXISTS idx_cochanges_a ON cochanges(file_a);
	CREATE INDEX IF NOT EXISTS idx_cochanges_b ON cochanges(file_b);

	-- C7 (RF-4): transitive-closure sidecar over VERIFIED edges only.
	-- A row (source_id, target_id, depth, min_confidence) means source_id
	-- transitively reaches target_id in depth hops, where min_confidence is
	-- the weakest edge confidence along that path. Built offline by the
	-- closure package after CALLS resolution; read by impact/trace via an
	-- indexed SELECT (depth<=3, min_confidence>=0.5). Absence of this table on
	-- an old graph.db triggers the Python live-BFS fallback (zero regression).
	CREATE TABLE IF NOT EXISTS closure (
		source_id INTEGER,
		target_id INTEGER,
		depth INTEGER,
		min_confidence REAL,
		PRIMARY KEY(source_id, target_id, depth)
	);
	CREATE INDEX IF NOT EXISTS idx_closure_source ON closure(source_id);
	CREATE INDEX IF NOT EXISTS idx_closure_target ON closure(target_id);

	`
	_, err := db.Exec(schema)
	if err != nil {
		return err
	}

	// FTS5 virtual table: SEPARATE from main schema because some SQLite builds
	// (e.g. container images) lack FTS5 support. Non-fatal: if FTS5 is unavailable,
	// the Python localizer falls back to name-match-only seeding.
	_, ftsErr := db.Exec(`
		CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
			name, qualified_name, signature, file_path,
			content='nodes', content_rowid='id'
		);
	`)
	if ftsErr != nil {
		log.Printf("[WARN] FTS5 not available (non-fatal): %v", ftsErr)
	}
	return nil
}

// PopulateFTS5 fills the nodes_fts virtual table from the nodes table.
// Must be called AFTER all nodes are inserted (end of DEFINITIONS pass).
// Idempotent: DELETEs existing FTS5 content first, then re-inserts.
// Non-fatal: if FTS5 table doesn't exist (SQLite build without FTS5),
// logs a warning and returns nil. The Python localizer handles this
// gracefully with name-match-only fallback.
func (d *DB) PopulateFTS5() error {
	// Check if nodes_fts table exists (FTS5 may not be compiled in)
	var count int
	err := d.db.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='nodes_fts'").Scan(&count)
	if err != nil || count == 0 {
		log.Printf("[WARN] nodes_fts table not found (FTS5 unavailable); skipping FTS5 population")
		return nil
	}
	// Clear any existing content (idempotent rebuild).
	if _, err := d.db.Exec("DELETE FROM nodes_fts"); err != nil {
		return fmt.Errorf("clear nodes_fts: %w", err)
	}
	_, err = d.db.Exec(
		`INSERT INTO nodes_fts(rowid, name, qualified_name, signature, file_path)
		 SELECT id, name, COALESCE(qualified_name, ''), COALESCE(signature, ''), file_path
		 FROM nodes`,
	)
	if err != nil {
		return fmt.Errorf("populate nodes_fts: %w", err)
	}
	return nil
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
		`INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence, metadata,
		 trust_tier, candidate_count, evidence_type, verification_status)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		e.SourceID, e.TargetID, e.Type, e.SourceLine, e.SourceFile, e.ResolutionMethod, e.Confidence, e.Metadata,
		e.TrustTier, e.CandidateCount, e.EvidenceType, e.VerificationStatus,
	)
	return err
}

// InsertFileHash records a file's content hash for incremental reindexing.
//
// RC-17 (F-004): the ``indexed_at`` column is wall-clock by default, which
// makes ``graph.db`` non-byte-deterministic across two builds. When
// ``GT_INDEX_FIXED_TS`` is set in the environment we use that literal
// value instead — enables the deterministic-build CI test (build twice,
// assert byte equality on every column except whatever the test
// explicitly excludes). Format expectation: RFC3339 UTC (caller's
// responsibility; we copy verbatim).
func (d *DB) InsertFileHash(filePath, hash, language string) error {
	ts := os.Getenv("GT_INDEX_FIXED_TS")
	if ts == "" {
		ts = time.Now().UTC().Format(time.RFC3339)
	}
	_, err := d.db.Exec(
		`INSERT OR REPLACE INTO file_hashes (file_path, content_hash, language, indexed_at) VALUES (?, ?, ?, ?)`,
		filePath, hash, language, ts,
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
			tx.Rollback()
			return nil, fmt.Errorf("last insert id for node %d: %w", i, err)
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
		 resolution_method, confidence, metadata, trust_tier, candidate_count, evidence_type, verification_status)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
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
			e.TrustTier, e.CandidateCount, e.EvidenceType, e.VerificationStatus,
		)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("insert edge %d: %w", i, err)
		}
	}
	return tx.Commit()
}

// GetAllEdges returns every edge whose confidence is >= minConf, in stable id
// order. Used by the C7 closure pass to build its adjacency list. The closure
// package applies an additional verified-resolution-method override on top of
// this floor (RF-4), so passing a conservative minConf here (e.g. 0.0) is
// safe — the closure filter is the authoritative gate. resolution_method and
// confidence may be NULL on rows from very old binaries; COALESCE keeps the
// scan total.
func (d *DB) GetAllEdges(minConf float64) ([]*Edge, error) {
	rows, err := d.db.Query(
		`SELECT id, source_id, target_id, type, source_line, COALESCE(source_file, ''),
		        COALESCE(resolution_method, ''), COALESCE(confidence, 0.0)
		   FROM edges
		  WHERE COALESCE(confidence, 0.0) >= ?`,
		minConf,
	)
	if err != nil {
		return nil, fmt.Errorf("query all edges: %w", err)
	}
	defer rows.Close()

	var edges []*Edge
	for rows.Next() {
		var e Edge
		if err := rows.Scan(&e.ID, &e.SourceID, &e.TargetID, &e.Type, &e.SourceLine,
			&e.SourceFile, &e.ResolutionMethod, &e.Confidence); err != nil {
			return nil, fmt.Errorf("scan edge: %w", err)
		}
		edges = append(edges, &e)
	}
	return edges, rows.Err()
}

// BatchInsertClosure inserts transitive-closure rows in a single transaction
// with a prepared statement, mirroring BatchInsertEdges. INSERT OR REPLACE so
// the (source_id, target_id, depth) primary key dedups deterministically if a
// row is somehow emitted twice.
func (d *DB) BatchInsertClosure(rows []*Closure) error {
	if len(rows) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	stmt, err := tx.Prepare(
		`INSERT OR REPLACE INTO closure (source_id, target_id, depth, min_confidence)
		 VALUES (?, ?, ?, ?)`,
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	for i, r := range rows {
		if _, err := stmt.Exec(r.SourceID, r.TargetID, r.Depth, r.MinConfidence); err != nil {
			tx.Rollback()
			return fmt.Errorf("insert closure %d: %w", i, err)
		}
	}
	return tx.Commit()
}

// ClosureCount returns total number of closure rows.
func (d *DB) ClosureCount() int {
	var count int
	d.db.QueryRow(`SELECT COUNT(*) FROM closure`).Scan(&count)
	return count
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
		`INSERT INTO assertions (test_node_id, target_node_id, resolution_score, kind, expression, expected, line) VALUES (?, ?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	for i, a := range assertions {
		_, err := stmt.Exec(a.TestNodeID, a.TargetNodeID, a.ResolutionScore, a.Kind, a.Expression, a.Expected, a.Line)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("insert assertion %d: %w", i, err)
		}
	}
	return tx.Commit()
}

// BatchInsertCochanges inserts co-change pairs in a single transaction.
// pairs maps [file_a, file_b] (canonical order) to co-occurrence count.
func (d *DB) BatchInsertCochanges(pairs map[[2]string]int) error {
	if len(pairs) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return err
	}
	stmt, err := tx.Prepare("INSERT OR REPLACE INTO cochanges (file_a, file_b, count) VALUES (?, ?, ?)")
	if err != nil {
		tx.Rollback()
		return err
	}
	defer stmt.Close()
	for pair, count := range pairs {
		if _, err := stmt.Exec(pair[0], pair[1], count); err != nil {
			tx.Rollback()
			return err
		}
	}
	return tx.Commit()
}
