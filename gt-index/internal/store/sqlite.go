// Package store handles SQLite graph database operations.
package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"log"
	"os"
	"sort"
	"strconv"
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

func nullableParentID(parentID int64) any {
	if parentID <= 0 {
		return nil
	}
	return parentID
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

// ResolutionCandidate is one viable target identity retained for a CALLS
// resolution. The selected edge remains in edges; this sidecar preserves all
// alternatives and their provenance for ambiguity-aware consumers.
type ResolutionCandidate struct {
	CallsiteID         string
	TargetID           int64
	TargetStableID     string
	TargetNativeID     string
	Ordinal            int
	Mechanism          string
	DeclaredScope      string
	ReceiverType       string
	ReceiverOrigin     string
	ReceiverShape      string
	ReceiverChain      string
	ImportChain        string
	DynamicDispatch    bool
	ExportStatus       string
	ParserComplete     *bool
	VerificationStatus string
	Selected           bool
	// Legacy native projections retained for the additive SQLite sidecar writer.
	SourceID         int64
	SourceLine       int
	SourceFile       string
	ResolutionMethod string
	CandidateRank    int
	CandidateCount   int
	Confidence       float64
	EvidenceType     string
}

// ResolutionCallsite is the lossless call-level dispatch record. Unlike an
// edge, selected_target_id is nullable for ambiguous and unresolved calls.
type ResolutionCallsite struct {
	CallsiteID                   string
	CallsiteOrdinal              int
	RepositoryRevision           string
	SourceStableID               string
	SourceNativeID               string
	SourceID                     int64
	SourceLine                   int
	SourceFile                   string
	Callee                       string
	Language                     string
	DispatchState                string
	CandidateCount               int
	SelectedTargetStableID       *string
	SelectedTargetNativeID       *string
	Mechanism                    string
	VerificationStatus           string
	LegacyReportedCandidateCount *int
	LegacySelectedNativeTargetID string
	// Legacy projections used by the v1 sidecar writer; the contract fields
	// above remain authoritative for HAR-6 consumers.
	CalleeQualified  string
	SelectedTargetID *int64
	ParserComplete   bool
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
//
// B-22 provenance fields (PropertyID … SourceRevision) carry per-fact identity, span, and
// authority so a property fact can be receipted/invalidated downstream exactly like an edge.
// They are DEFAULTED deterministically by fillProvenance() at write time when a caller
// leaves them zero, so the parser/main path can keep populating only the original five
// fields (NodeID, Kind, Value, Line, Confidence) and still get full provenance.
type Property struct {
	ID         int64
	NodeID     int64
	Kind       string // guard_clause, return_shape, exception_type, docstring, caller_usage, conditional_return, side_effect, param, security_tag, exception_flow, exception_handler, fingerprint, field_read, boundary_condition, class_field, class_decorator
	Value      string
	Line       int
	Confidence float64
	// B-22 provenance (defaulted at write time when unset):
	PropertyID         string // stable content-hash fact id (hex sha256 of node_id|kind|value|line)
	StartLine          int    // span start (defaults to Line)
	EndLine            int    // span end (defaults to Line)
	Extractor          string // which extractor produced it (default "treesitter")
	EvidenceMethod     string // how it was derived (default "ast")
	TrustTier          string // CERTIFIED/CANDIDATE/SPECULATIVE (default tierForConfidence(Confidence))
	VerificationStatus string // default "unverified"
	SourceRevision     string // composite graph revision the fact was mined under (back-filled post-hash)
}

// propertyFactID is the stable, id-INDEPENDENT-of-AUTOINCREMENT content id of a property
// fact: hex SHA-256 of the length-framed (node_id, kind, value, line) tuple. Deterministic,
// collision-safe framing (revEncodeRecord). Distinct facts get distinct ids; the same fact
// re-extracted gets the same id, so a consumer can dedup/receipt it.
func propertyFactID(nodeID int64, kind, value string, line int) string {
	sum := sha256.Sum256(revEncodeRecord(
		strconv.FormatInt(nodeID, 10), kind, value, strconv.Itoa(line),
	))
	return hex.EncodeToString(sum[:])
}

// fillProvenance defaults any unset B-22 provenance field deterministically. Idempotent:
// a caller that already set a field keeps it. Trust tier mirrors edges' tierFor thresholds
// (tierForConfidence) so a property carries the same correct-or-quiet authority as an edge.
func (p *Property) fillProvenance() {
	if p.StartLine == 0 {
		p.StartLine = p.Line
	}
	if p.EndLine == 0 {
		p.EndLine = p.Line
	}
	if p.EndLine < p.StartLine {
		p.EndLine = p.StartLine
	}
	if p.Extractor == "" {
		p.Extractor = "treesitter"
	}
	if p.EvidenceMethod == "" {
		p.EvidenceMethod = "ast"
	}
	if p.TrustTier == "" {
		p.TrustTier = tierForConfidence(p.Confidence)
	}
	if p.VerificationStatus == "" {
		p.VerificationStatus = "unverified"
	}
	if p.PropertyID == "" {
		p.PropertyID = propertyFactID(p.NodeID, p.Kind, p.Value, p.Line)
	}
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
		return fmt.Errorf("%d foreign key violations found in graph.db", violations)
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
		parent_id INTEGER REFERENCES nodes(id),
		-- SM-9a MULTI-REPO: nullable repo partition key. NULL on single-root indexes
		-- (byte-identical to pre-multi-repo binaries — the column is added at the END,
		-- existing INSERTs omit it, so it stays NULL and no pre-existing query is
		-- perturbed). Populated only on the multi-root path (repos.id). file_path alone
		-- is NOT a global locator once >1 repo is indexed (two repos' identical relative
		-- paths collide); (repo_id, file_path) is.
		repo_id INTEGER REFERENCES repos(id)
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
		verification_status TEXT DEFAULT 'unverified',
		-- SM-9a MULTI-REPO: nullable repo of the edge's SOURCE (the calling/importing
		-- repo). A cross-repo edge is one whose source repo != target's repo; carry the
		-- source repo here and the target's provenance in metadata. NULL on single-root.
		repo_id INTEGER REFERENCES repos(id)
	);

	-- Versioned additive sidecar: one row for every viable resolver candidate.
	-- edges.target_id remains the backward-readable selected edge; consumers that
	-- understand this table can distinguish unique, ambiguous, and unresolved
	-- identity without inferring from a lossy count.
	CREATE TABLE IF NOT EXISTS resolution_candidates (
		callsite_id TEXT NOT NULL REFERENCES resolution_callsites(callsite_id) ON DELETE CASCADE,
		target_id INTEGER NOT NULL REFERENCES nodes(id),
		target_stable_id TEXT NOT NULL REFERENCES resolution_symbols(stable_id),
		target_native_id TEXT NOT NULL,
		ordinal INTEGER NOT NULL,
		mechanism TEXT NOT NULL,
		declared_scope TEXT NOT NULL DEFAULT '',
		receiver_type TEXT NOT NULL DEFAULT '',
		receiver_origin TEXT NOT NULL DEFAULT '',
		receiver_shape TEXT NOT NULL DEFAULT '',
		receiver_chain TEXT NOT NULL DEFAULT '[]',
		import_chain TEXT NOT NULL DEFAULT '[]',
		dynamic_dispatch BOOLEAN NOT NULL DEFAULT 0,
		export_status TEXT NOT NULL DEFAULT 'unknown',
		parser_complete BOOLEAN,
		verification_status TEXT NOT NULL,
		selected BOOLEAN NOT NULL DEFAULT 0,
		PRIMARY KEY(callsite_id, ordinal),
		UNIQUE(callsite_id, target_stable_id)
	);
	CREATE INDEX IF NOT EXISTS idx_resolution_candidates_callsite ON resolution_candidates(callsite_id);
	CREATE INDEX IF NOT EXISTS idx_resolution_candidates_target ON resolution_candidates(target_id);

	CREATE TABLE IF NOT EXISTS resolution_symbols (
		stable_id TEXT PRIMARY KEY,
		native_id TEXT NOT NULL UNIQUE,
		native_kind TEXT NOT NULL,
		normalized_kind TEXT NOT NULL,
		language TEXT NOT NULL,
		path TEXT NOT NULL,
		qualified_name TEXT NOT NULL,
		start_line INTEGER NOT NULL,
		end_line INTEGER NOT NULL,
		export_status TEXT NOT NULL
	);

	CREATE TABLE IF NOT EXISTS resolution_callsites (
		callsite_id TEXT PRIMARY KEY,
		callsite_ordinal INTEGER NOT NULL UNIQUE,
		repository_revision TEXT NOT NULL,
		source_stable_id TEXT NOT NULL REFERENCES resolution_symbols(stable_id),
		source_native_id TEXT NOT NULL,
		source_id INTEGER,
		source_line INTEGER,
		source_file TEXT NOT NULL,
		callee TEXT NOT NULL,
		language TEXT NOT NULL,
		dispatch_state TEXT NOT NULL,
		candidate_count INTEGER NOT NULL DEFAULT 0,
		selected_target_stable_id TEXT,
		selected_target_native_id TEXT,
		mechanism TEXT NOT NULL,
		verification_status TEXT NOT NULL DEFAULT 'unverified',
		legacy_reported_candidate_count INTEGER,
		legacy_selected_native_target_id TEXT NOT NULL DEFAULT ''
	);
	CREATE INDEX IF NOT EXISTS idx_resolution_callsites_source ON resolution_callsites(source_id);

	-- SM-9a MULTI-REPO: one row per indexed repository root. id is the partition key
	-- stamped onto nodes/edges/properties/assertions/closure/content_passages.repo_id.
	-- root = the repo's on-disk root; "commit" = its VCS HEAD (best-effort, "" when
	-- unavailable). Empty on a single-root index (that path never writes a repos row),
	-- so existing single-repo consumers see an empty table and are unaffected.
	CREATE TABLE IF NOT EXISTS repos (
		id INTEGER PRIMARY KEY,
		root TEXT,
		"commit" TEXT
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
		confidence REAL DEFAULT 1.0,
		-- B-22 per-fact provenance (added to fresh dbs here; migrateSchema ALTERs old dbs):
		property_id TEXT,
		start_line INTEGER,
		end_line INTEGER,
		extractor TEXT,
		evidence_method TEXT,
		trust_tier TEXT,
		verification_status TEXT DEFAULT 'unverified',
		source_revision TEXT,
		-- SM-9a MULTI-REPO: nullable repo of the owning node (back-filled from node_id).
		repo_id INTEGER REFERENCES repos(id)
	);

	CREATE TABLE IF NOT EXISTS assertions (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		test_node_id INTEGER NOT NULL REFERENCES nodes(id),
		target_node_id INTEGER DEFAULT 0,
		resolution_score REAL DEFAULT 0.0,
		kind TEXT NOT NULL,
		expression TEXT NOT NULL,
		expected TEXT,
		line INTEGER,
		-- SM-9a MULTI-REPO: nullable repo of the test node (back-filled from test_node_id).
		repo_id INTEGER REFERENCES repos(id)
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

	-- DCC (Dynamic Concern Consensus) set-form co-change. SEPARATE from the legacy
	-- pair table cochanges above (which stays bit-for-bit): stores, for each
	-- base-ancestor commit touching <=50 files, the SET of member paths plus the
	-- commit-hash witness. No scores / decay / caps -- pure set membership. The
	-- Python DCC producer reads the git-provenance axis from here; absence (an old
	-- graph) simply leaves that axis empty (correct-or-quiet).
	CREATE TABLE IF NOT EXISTS cochange_sets (
		commit_hash TEXT NOT NULL,
		file_path TEXT NOT NULL,
		PRIMARY KEY(commit_hash, file_path)
	);
	CREATE INDEX IF NOT EXISTS idx_cochange_sets_file ON cochange_sets(file_path);
	CREATE INDEX IF NOT EXISTS idx_cochange_sets_commit ON cochange_sets(commit_hash);

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
		-- SM-9a MULTI-REPO: nullable repo of the closure SOURCE (back-filled from source_id).
		repo_id INTEGER REFERENCES repos(id),
		PRIMARY KEY(source_id, target_id, depth)
	);
	CREATE INDEX IF NOT EXISTS idx_closure_source ON closure(source_id);
	CREATE INDEX IF NOT EXISTS idx_closure_target ON closure(target_id);

	CREATE INDEX IF NOT EXISTS idx_properties_property_id ON properties(property_id);

	-- B-24: normalized, schema-versioned edge metadata. edges.metadata is a polymorphic
	-- string (route JSON on API edges vs semicolon-separated key=value on promoted CALLS),
	-- so consumers regex/LIKE it. This DERIVED sub-table (populated by PopulateEdgeMetadata
	-- via the ONE canonical ParseEdgeMetadata parser) exposes each field as an individually
	-- queryable row: SELECT value FROM edge_metadata WHERE edge_id=? AND key='receiver_type'.
	-- edges.metadata stays byte-identical (the receiver-provenance + incremental-restore
	-- invariants are untouched); this is an additive index over it. Absent on an old graph.db
	-- -> consumers fall back to parsing the string (correct-or-quiet).
	CREATE TABLE IF NOT EXISTS edge_metadata (
		edge_id INTEGER NOT NULL,
		key TEXT NOT NULL,
		value TEXT NOT NULL,
		schema_version INTEGER NOT NULL DEFAULT 1,
		PRIMARY KEY(edge_id, key)
	);
	CREATE INDEX IF NOT EXISTS idx_edge_metadata_key ON edge_metadata(key);

	-- B-23: passage-level content identity. symbol_content_fts keys a whole body by node
	-- rowid, so a body-concept match can only cite the node start_line. content_passages
	-- stores each non-blank body line as an addressable span (node_id, start_line, end_line,
	-- content, content_hash); content_passages_fts (created in the FTS block, FTS5-gated)
	-- indexes it so a match resolves to the EXACT matching line, not the node start_line.
	CREATE TABLE IF NOT EXISTS content_passages (
		passage_id INTEGER PRIMARY KEY AUTOINCREMENT,
		node_id INTEGER NOT NULL,
		start_line INTEGER NOT NULL,
		end_line INTEGER NOT NULL,
		content TEXT NOT NULL,
		content_hash TEXT NOT NULL,
		-- SM-9a MULTI-REPO: nullable repo of the owning node (back-filled from node_id).
		repo_id INTEGER REFERENCES repos(id)
	);
	CREATE INDEX IF NOT EXISTS idx_content_passages_node ON content_passages(node_id);

	`
	_, err := db.Exec(schema)
	if err != nil {
		return err
	}
	if err := migrateSchema(db); err != nil {
		return fmt.Errorf("migrate schema: %w", err)
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
	// B-23: FTS5 over per-body-line passages. rowid == content_passages.passage_id so a
	// body-concept MATCH resolves to the exact matching line's span. Same tokenizer as
	// symbol_content_fts (unicode61 tokenchars '_') so BM25 semantics are consistent across
	// the two content surfaces. Regular (self-content) fts5 — DELETE+reinsert rebuild, the
	// proven symbol_content_fts pattern. FTS5-gated + non-fatal, exactly like nodes_fts:
	// absent when the binary lacks the sqlite_fts5 tag (passage citation then degrades to
	// the node start_line — correct-or-quiet).
	_, pftsErr := db.Exec(
		`CREATE VIRTUAL TABLE IF NOT EXISTS content_passages_fts USING fts5(content, tokenize="unicode61 tokenchars '_'")`,
	)
	if pftsErr != nil {
		log.Printf("[WARN] content_passages_fts not available (non-fatal): %v", pftsErr)
	}
	return nil
}

// migrateSchema brings an existing (older) graph.db up to the current column set WITHOUT a
// destructive rebuild: `CREATE TABLE IF NOT EXISTS` is a no-op on a table that already
// exists, so new columns on a pre-existing table must be added by ALTER. Idempotent —
// addColumnIfMissing checks PRAGMA table_info first, so re-running on a current db does
// nothing. Back-compat: an old reader never sees these columns; a new reader COALESCEs them.
func migrateSchema(db *sql.DB) error {
	// B-22: property provenance columns on a properties table created by an old binary.
	propCols := []struct{ name, ddl string }{
		{"property_id", "TEXT"},
		{"start_line", "INTEGER"},
		{"end_line", "INTEGER"},
		{"extractor", "TEXT"},
		{"evidence_method", "TEXT"},
		{"trust_tier", "TEXT"},
		{"verification_status", "TEXT DEFAULT 'unverified'"},
		{"source_revision", "TEXT"},
	}
	for _, c := range propCols {
		if err := addColumnIfMissing(db, "properties", c.name, c.ddl); err != nil {
			return err
		}
	}
	// SM-9a MULTI-REPO: add the nullable repo_id partition key to a fact table created by
	// an older binary. Additive + idempotent (addColumnIfMissing checks PRAGMA table_info
	// first): a single-root db written by an old binary reopens with repo_id present-and-NULL,
	// so every existing query that omits repo_id is unchanged. The repos table itself is
	// created by createSchema's `CREATE TABLE IF NOT EXISTS repos` above (a no-op when present).
	for _, t := range []string{"nodes", "edges", "properties", "assertions", "closure", "content_passages"} {
		if err := addColumnIfMissing(db, t, "repo_id", "INTEGER"); err != nil {
			return err
		}
	}
	return nil
}

// addColumnIfMissing ALTERs `table` to add `col` (with `ddl` type/default) only when it is
// absent. Deterministic and safe to call on every Open.
func addColumnIfMissing(db *sql.DB, table, col, ddl string) error {
	rows, err := db.Query(fmt.Sprintf("PRAGMA table_info(%s)", table))
	if err != nil {
		return fmt.Errorf("table_info(%s): %w", table, err)
	}
	defer rows.Close()
	for rows.Next() {
		var (
			cid, notnull, pk int
			name, ctype      string
			dfltValue        sql.NullString
		)
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dfltValue, &pk); err != nil {
			return fmt.Errorf("scan table_info(%s): %w", table, err)
		}
		if name == col {
			return nil // already present
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if _, err := db.Exec(fmt.Sprintf("ALTER TABLE %s ADD COLUMN %s %s", table, col, ddl)); err != nil {
		return fmt.Errorf("add column %s.%s: %w", table, col, err)
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
	// LEAK INVARIANT (Fable S1/L2, reproduced on the live artifact): nodes_fts is the
	// localizer's stage-1 retrieval surface. Test symbols (Go `TestX`, Mocha `it: …`
	// descriptions) indexed here become FTS5_SEEDs/BFS roots and render agent-visibly as
	// `fts5 match: …` — surfacing test NAMES, the FAIL_TO_PASS surface. Exclude is_test at
	// INSERT (parity with symbol_content_fts's PopulateContentFTS, which already filters).
	insertSQL := `INSERT INTO nodes_fts(rowid, name, qualified_name, signature, file_path)
		 SELECT id, name, COALESCE(qualified_name, ''), COALESCE(signature, ''), file_path
		 FROM nodes WHERE COALESCE(is_test, 0) = 0`
	// Clear existing content (idempotent rebuild), then re-insert. Use the FTS5
	// content-INDEPENDENT 'delete-all' command, NOT `DELETE FROM nodes_fts` (Fable S5):
	// on this EXTERNAL-content table, a plain DELETE re-reads the (already-mutated by the
	// reindex/LSP pass) `nodes` content to locate rows and raises "database disk image is
	// malformed", forcing the O(all-nodes) DROP+recreate self-heal on EVERY -file reindex.
	// 'delete-all' clears the index directly, so the primary path stays incremental.
	_, delErr := d.db.Exec("INSERT INTO nodes_fts(nodes_fts) VALUES('delete-all')")
	if delErr == nil {
		if _, err := d.db.Exec(insertSQL); err == nil {
			return nil
		} else {
			delErr = err // fall through to the DROP+recreate recovery
		}
	}
	// Recovery: the external-content FTS5 index can be CORRUPT ("database disk image
	// is malformed") — e.g. when nodes was UPDATEd (LSP resolve) or graph.db was
	// `docker cp`'d away from its -wal between containers, stranding the index. DELETE
	// then fails. DROP the vtable and recreate it fresh from the current nodes table.
	log.Printf("[WARN] nodes_fts clear/insert failed (%v) — DROP+recreate to self-heal the FTS5 index", delErr)
	// R#5: the DROP+recreate+populate must be ATOMIC. Run as three autocommit Execs,
	// a failure AFTER the DROP (recreate or populate errors — disk full, malformed
	// content row, interrupted reindex) leaves nodes_fts DROPPED/empty, so every later
	// localizer query silently degrades to the name-match fallback with no error to the
	// caller. Wrap it in ONE transaction: any failure rolls back to the pre-heal state
	// (the original — still corrupt — index) and surfaces the error, rather than
	// shipping a half-applied, empty FTS5 surface.
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin nodes_fts self-heal tx: %w", err)
	}
	if _, err := tx.Exec("DROP TABLE IF EXISTS nodes_fts"); err != nil {
		tx.Rollback()
		return fmt.Errorf("drop corrupt nodes_fts: %w", err)
	}
	if _, err := tx.Exec(`CREATE VIRTUAL TABLE nodes_fts USING fts5(
		name, qualified_name, signature, file_path,
		content='nodes', content_rowid='id'
	)`); err != nil {
		tx.Rollback()
		return fmt.Errorf("recreate nodes_fts: %w", err)
	}
	if _, err := tx.Exec(insertSQL); err != nil {
		tx.Rollback()
		return fmt.Errorf("populate nodes_fts after recreate: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit nodes_fts self-heal: %w", err)
	}
	return nil
}

// FTS5RowCount returns the number of rows in nodes_fts, or -1 when the table
// is absent (the binary was built WITHOUT the `sqlite_fts5` build tag, so the
// CREATE VIRTUAL TABLE … fts5 was rejected). Used by the GT_REQUIRE_FTS5
// preflight gate in main to abort a paid run that would silently degrade to the
// Python name-match fallback. A real, queryable FTS5 index is the first stage of
// the localizer pipeline; we never want to pay for a run without it.
func (d *DB) FTS5RowCount() int {
	var n int
	if err := d.db.QueryRow("SELECT COUNT(*) FROM nodes_fts").Scan(&n); err != nil {
		return -1
	}
	return n
}

// InsertNode inserts a node and returns its ID.
func (d *DB) InsertNode(n *Node) (int64, error) {
	res, err := d.db.Exec(
		`INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line,
		 signature, return_type, is_exported, is_test, language, parent_id)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		n.Label, n.Name, n.QualifiedName, n.FilePath, n.StartLine, n.EndLine,
		n.Signature, n.ReturnType, n.IsExported, n.IsTest, n.Language, nullableParentID(n.ParentID),
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
// RC-17 (F-004): the `indexed_at` column is wall-clock by default, which
// makes `graph.db` non-byte-deterministic across two builds. When
// `GT_INDEX_FIXED_TS` is set in the environment we use that literal
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
			n.Signature, n.ReturnType, n.IsExported, n.IsTest, n.Language, nullableParentID(n.ParentID),
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

// BatchInsertResolutionCandidates persists the complete viable candidate set
// for each resolved call. Inputs are sorted by source/location/rank by the
// caller, so repeated builds produce deterministic row order.
func (d *DB) BatchInsertResolutionCandidates(candidates []*ResolutionCandidate) error {
	if len(candidates) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin resolution candidates tx: %w", err)
	}
	stmt, err := tx.Prepare(`INSERT OR REPLACE INTO resolution_candidates
		(callsite_id,target_id,target_stable_id,target_native_id,ordinal,mechanism,
		 declared_scope,receiver_type,receiver_origin,receiver_shape,receiver_chain,
		 import_chain,dynamic_dispatch,export_status,parser_complete,
		 verification_status,selected)
		 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare resolution candidates: %w", err)
	}
	defer stmt.Close()
	for i, c := range candidates {
		if _, err := stmt.Exec(c.CallsiteID, c.TargetID, c.TargetStableID,
			c.TargetNativeID, c.Ordinal, c.Mechanism, c.DeclaredScope,
			c.ReceiverType, c.ReceiverOrigin, c.ReceiverShape, c.ReceiverChain,
			c.ImportChain, c.DynamicDispatch, c.ExportStatus, c.ParserComplete,
			c.VerificationStatus, c.Selected); err != nil {
			tx.Rollback()
			return fmt.Errorf("insert resolution candidate %d: %w", i, err)
		}
	}
	return tx.Commit()
}

// BatchInsertResolutionCallsites writes one durable dispatch record per parsed
// callsite, including calls for which no edge was emitted.
func (d *DB) BatchInsertResolutionCallsites(callsites []*ResolutionCallsite) error {
	if len(callsites) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin resolution callsites tx: %w", err)
	}
	stmt, err := tx.Prepare(`INSERT OR REPLACE INTO resolution_callsites
		(callsite_id,callsite_ordinal,repository_revision,source_stable_id,
		 source_native_id,source_id,source_line,source_file,callee,language,
		 dispatch_state,candidate_count,selected_target_stable_id,
		 selected_target_native_id,mechanism,verification_status,
		 legacy_reported_candidate_count,legacy_selected_native_target_id)
		 VALUES (?,?,?,?,?,NULLIF(?,0),?,?,?,?,?,?,?,?,?,?,?,?)`)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare resolution callsites: %w", err)
	}
	defer stmt.Close()
	for i, c := range callsites {
		if _, err := stmt.Exec(c.CallsiteID, c.CallsiteOrdinal, c.RepositoryRevision,
			c.SourceStableID, c.SourceNativeID, c.SourceID, c.SourceLine,
			c.SourceFile, c.Callee, c.Language, c.DispatchState, c.CandidateCount,
			c.SelectedTargetStableID, c.SelectedTargetNativeID, c.Mechanism,
			c.VerificationStatus, c.LegacyReportedCandidateCount,
			c.LegacySelectedNativeTargetID); err != nil {
			tx.Rollback()
			return fmt.Errorf("insert resolution callsite %d: %w", i, err)
		}
	}
	return tx.Commit()
}

func (d *DB) BatchInsertResolutionSymbols(symbols []*ResolutionSymbol) error {
	if len(symbols) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin resolution symbols tx: %w", err)
	}
	stmt, err := tx.Prepare(`INSERT OR REPLACE INTO resolution_symbols
		(stable_id,native_id,native_kind,normalized_kind,language,path,
		 qualified_name,start_line,end_line,export_status) VALUES (?,?,?,?,?,?,?,?,?,?)`)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare resolution symbols: %w", err)
	}
	defer stmt.Close()
	for i, symbol := range symbols {
		if _, err := stmt.Exec(symbol.StableID, symbol.NativeID, symbol.NativeKind,
			symbol.NormalizedKind, symbol.Language, symbol.Path, symbol.QualifiedName,
			symbol.StartLine, symbol.EndLine, symbol.ExportStatus); err != nil {
			tx.Rollback()
			return fmt.Errorf("insert resolution symbol %d: %w", i, err)
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
		  WHERE COALESCE(confidence, 0.0) >= ?
		  ORDER BY id`,
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

// ClearClosure deletes all rows from the closure table. Required before a
// closure REBUILD (the post-LSP -rebuild-closure pass): BatchInsertClosure
// uses INSERT OR REPLACE keyed on (source_id, target_id, depth), so without a
// clear it would leave behind stale pairs — reachability through edges the LSP
// resolve pass later DELETED or RE-POINTED (e.g. hono lost 1364 false pairs).
// A clean clear makes the recomputed closure exactly reflect the current edges.
func (d *DB) ClearClosure() error {
	_, err := d.db.Exec(`DELETE FROM closure`)
	return err
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
	d.db.Exec("UPDATE nodes SET parent_id = ? WHERE id = ?", nullableParentID(parentID), nodeID)
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

// InsertProperty inserts a property for a node. Provenance columns (B-22) are defaulted
// deterministically by fillProvenance() when the caller leaves them zero.
func (d *DB) InsertProperty(p *Property) error {
	p.fillProvenance()
	_, err := d.db.Exec(
		`INSERT INTO properties (node_id, kind, value, line, confidence,
		   property_id, start_line, end_line, extractor, evidence_method,
		   trust_tier, verification_status, source_revision)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		p.NodeID, p.Kind, p.Value, p.Line, p.Confidence,
		p.PropertyID, p.StartLine, p.EndLine, p.Extractor, p.EvidenceMethod,
		p.TrustTier, p.VerificationStatus, p.SourceRevision,
	)
	return err
}

// InsertAssertion inserts an assertion from a test function.
func (d *DB) InsertAssertion(a *Assertion) error {
	// Column set MUST match BatchInsertAssertions (incl. resolution_score, the
	// multi-signal link score); omitting it silently defaulted every single-row
	// assertion to resolution_score = 0.0.
	_, err := d.db.Exec(
		`INSERT INTO assertions (test_node_id, target_node_id, resolution_score, kind, expression, expected, line) VALUES (?, ?, ?, ?, ?, ?, ?)`,
		a.TestNodeID, a.TargetNodeID, a.ResolutionScore, a.Kind, a.Expression, a.Expected, a.Line,
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
		`INSERT INTO properties (node_id, kind, value, line, confidence,
		   property_id, start_line, end_line, extractor, evidence_method,
		   trust_tier, verification_status, source_revision)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	for i, p := range props {
		p.fillProvenance()
		_, err := stmt.Exec(p.NodeID, p.Kind, p.Value, p.Line, p.Confidence,
			p.PropertyID, p.StartLine, p.EndLine, p.Extractor, p.EvidenceMethod,
			p.TrustTier, p.VerificationStatus, p.SourceRevision)
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
	// DETERMINISM (Fable S3): iterate pairs in SORTED order, never Go map-range order —
	// SQLite assigns rowids in insertion order, so a randomized map range makes two
	// indexings of the SAME repo produce byte-DIFFERENT graph.db files. This function
	// violated the exact invariant its sibling BatchInsertCochangeSets documents+enforces.
	keys := make([][2]string, 0, len(pairs))
	for pair := range pairs {
		keys = append(keys, pair)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i][0] != keys[j][0] {
			return keys[i][0] < keys[j][0]
		}
		return keys[i][1] < keys[j][1]
	})
	for _, pair := range keys {
		if _, err := stmt.Exec(pair[0], pair[1], pairs[pair]); err != nil {
			tx.Rollback()
			return err
		}
	}
	return tx.Commit()
}

// BatchInsertCochangeSets inserts set-form co-change membership in one transaction.
// sets maps a commit hash to the member file paths that commit touched (set-form,
// no scores). Rows are (commit_hash, file_path); INSERT OR IGNORE keeps the
// (commit_hash, file_path) primary key unique. SEPARATE from BatchInsertCochanges
// — the legacy pair table is never touched.
func (d *DB) BatchInsertCochangeSets(sets map[string][]string) error {
	if len(sets) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return err
	}
	stmt, err := tx.Prepare("INSERT OR IGNORE INTO cochange_sets (commit_hash, file_path) VALUES (?, ?)")
	if err != nil {
		tx.Rollback()
		return err
	}
	defer stmt.Close()
	// DETERMINISM: iterate commits in SORTED order, never Go map-range order. SQLite
	// assigns rowids in insertion order, so a randomized map range makes two indexings
	// of the SAME repo produce byte-DIFFERENT graph.db files (invariant: graph.db is
	// byte-identical across two indexings). Files within a commit keep their given
	// order (git-log --name-only order, already deterministic per commit).
	hashes := make([]string, 0, len(sets))
	for h := range sets {
		hashes = append(hashes, h)
	}
	sort.Strings(hashes)
	for _, commit := range hashes {
		for _, f := range sets[commit] {
			if _, err := stmt.Exec(commit, f); err != nil {
				tx.Rollback()
				return err
			}
		}
	}
	return tx.Commit()
}

// ────────────────────────────────────────────────────────────────────────────
// SM-9a MULTI-REPO helpers. All of these operate on the multi-root ingest path
// ONLY — the single-root path never calls them, so a single-repo index leaves
// every repo_id NULL and the repos table empty (byte-identical to the pre-multi-
// repo binary on every pre-existing column). Additive by construction.
// ────────────────────────────────────────────────────────────────────────────

// CrossRepoNodeRow is the minimal node projection the cross-repo import resolver
// needs: identity (id), the repo it lives in, its name, its repo-relative file
// path, and whether it is exported (only exported symbols are legal cross-repo
// import targets). Label lets the caller drop File anchors.
type CrossRepoNodeRow struct {
	ID         int64
	RepoID     int64
	Name       string
	FilePath   string
	Label      string
	IsExported bool
}

// UpsertRepo writes (or replaces) one row in the repos partition table. Called
// once per indexed root on the multi-repo path with id = the repo's partition key.
func (d *DB) UpsertRepo(id int64, root, commit string) error {
	_, err := d.db.Exec(
		`INSERT OR REPLACE INTO repos (id, root, "commit") VALUES (?, ?, ?)`,
		id, root, commit,
	)
	return err
}

// RepoCount returns the number of rows in the repos table (0 on a single-root index).
func (d *DB) RepoCount() int {
	var n int
	d.db.QueryRow(`SELECT COUNT(*) FROM repos`).Scan(&n)
	return n
}

// StampNodeRepoIDs sets nodes.repo_id = repoID for the given node ids in ONE
// transaction. Called once per repo on the multi-repo path with the exact id list
// BatchInsertNodes returned for that repo, so no file_path-collision ambiguity can
// arise (the stamp is keyed on the global surrogate id, never the colliding path).
func (d *DB) StampNodeRepoIDs(ids []int64, repoID int64) error {
	if len(ids) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin stamp-node-repo tx: %w", err)
	}
	stmt, err := tx.Prepare(`UPDATE nodes SET repo_id = ? WHERE id = ?`)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare stamp-node-repo: %w", err)
	}
	defer stmt.Close()
	for _, id := range ids {
		if _, err := stmt.Exec(repoID, id); err != nil {
			tx.Rollback()
			return fmt.Errorf("stamp node %d repo_id: %w", id, err)
		}
	}
	return tx.Commit()
}

// StampDerivedRepoIDs back-fills repo_id on every fact surface that references a
// node, deriving it from that node's already-stamped repo_id. Node ids are GLOBAL
// (never collide across repos), so these joins are unambiguous even where two repos
// share a relative file_path:
//   - edges       ← source_id (the calling/importing repo; a cross-repo edge is one
//     whose source repo != its target's repo)
//   - properties  ← node_id
//   - assertions  ← test_node_id
//   - closure     ← source_id
//   - content_passages ← node_id
//
// Run AFTER StampNodeRepoIDs for every repo and AFTER all edges (intra- and
// cross-repo) are inserted. Deterministic (set-based UPDATEs, no ordering).
func (d *DB) StampDerivedRepoIDs() error {
	stmts := []string{
		`UPDATE edges SET repo_id = (SELECT n.repo_id FROM nodes n WHERE n.id = edges.source_id)`,
		`UPDATE properties SET repo_id = (SELECT n.repo_id FROM nodes n WHERE n.id = properties.node_id)`,
		`UPDATE assertions SET repo_id = (SELECT n.repo_id FROM nodes n WHERE n.id = assertions.test_node_id)`,
		`UPDATE closure SET repo_id = (SELECT n.repo_id FROM nodes n WHERE n.id = closure.source_id)`,
		`UPDATE content_passages SET repo_id = (SELECT n.repo_id FROM nodes n WHERE n.id = content_passages.node_id)`,
	}
	for _, s := range stmts {
		if _, err := d.db.Exec(s); err != nil {
			return fmt.Errorf("stamp derived repo_id (%q): %w", s, err)
		}
	}
	return nil
}

// CrossRepoNodeRows returns the repo-stamped node projection the cross-repo import
// resolver consumes. File-label anchors and test nodes are excluded (an import
// never binds to a file anchor or a test symbol). Ordered by (repo_id, id) for
// deterministic downstream iteration.
func (d *DB) CrossRepoNodeRows() ([]CrossRepoNodeRow, error) {
	rows, err := d.db.Query(
		`SELECT id, COALESCE(repo_id, -1), name, file_path, label, COALESCE(is_exported, 0)
		   FROM nodes
		  WHERE label != 'File' AND COALESCE(is_test, 0) = 0
		  ORDER BY COALESCE(repo_id, -1), id`,
	)
	if err != nil {
		return nil, fmt.Errorf("query cross-repo nodes: %w", err)
	}
	defer rows.Close()
	var out []CrossRepoNodeRow
	for rows.Next() {
		var r CrossRepoNodeRow
		if err := rows.Scan(&r.ID, &r.RepoID, &r.Name, &r.FilePath, &r.Label, &r.IsExported); err != nil {
			return nil, fmt.Errorf("scan cross-repo node: %w", err)
		}
		out = append(out, r)
	}
	return out, rows.Err()
}
