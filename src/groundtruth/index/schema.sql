CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    language TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line_number INTEGER,
    end_line INTEGER,
    is_exported BOOLEAN DEFAULT FALSE,
    signature TEXT,
    params TEXT,
    return_type TEXT,
    documentation TEXT,
    usage_count INTEGER DEFAULT 0,
    last_indexed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    module_path TEXT NOT NULL,
    is_default BOOLEAN DEFAULT FALSE,
    is_named BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version TEXT,
    package_manager TEXT NOT NULL,
    is_dev_dependency BOOLEAN DEFAULT FALSE,
    UNIQUE(name, package_manager)
);

CREATE TABLE IF NOT EXISTS refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    referenced_in_file TEXT NOT NULL,
    referenced_at_line INTEGER,
    reference_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    tool TEXT NOT NULL,
    file_path TEXT,
    language TEXT,
    phase TEXT NOT NULL,
    outcome TEXT NOT NULL,
    errors_found INTEGER DEFAULT 0,
    errors_fixed INTEGER DEFAULT 0,
    error_types TEXT,
    ai_called BOOLEAN DEFAULT FALSE,
    ai_model TEXT,
    latency_ms INTEGER,
    tokens_used INTEGER DEFAULT 0,
    fix_accepted BOOLEAN,
    run_id TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_name_exported ON symbols(name) WHERE is_exported = TRUE;
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_language ON symbols(language);
CREATE INDEX IF NOT EXISTS idx_symbols_usage ON symbols(usage_count DESC);
CREATE INDEX IF NOT EXISTS idx_exports_module ON exports(module_path);
CREATE INDEX IF NOT EXISTS idx_packages_name ON packages(name);
CREATE INDEX IF NOT EXISTS idx_refs_symbol ON refs(symbol_id);
CREATE INDEX IF NOT EXISTS idx_refs_file ON refs(referenced_in_file);
CREATE INDEX IF NOT EXISTS idx_interventions_timestamp ON interventions(timestamp);
CREATE INDEX IF NOT EXISTS idx_interventions_run_id ON interventions(run_id);

-- Briefing logs for grounding gap analysis
CREATE TABLE IF NOT EXISTS briefing_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    intent TEXT NOT NULL,
    briefing_text TEXT NOT NULL,
    briefing_symbols TEXT NOT NULL,
    target_file TEXT,
    subsequent_validation_id INTEGER REFERENCES interventions(id),
    compliance_rate REAL,
    symbols_used_correctly TEXT,
    symbols_ignored TEXT,
    hallucinated_despite_briefing TEXT
);
CREATE INDEX IF NOT EXISTS idx_briefing_logs_timestamp ON briefing_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_briefing_logs_target ON briefing_logs(target_file);

-- Persistent index metadata for incremental re-indexing
CREATE TABLE IF NOT EXISTS index_metadata (
    file_path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    symbol_count INTEGER NOT NULL,
    indexed_at INTEGER NOT NULL
);

-- Key-value metadata for artifact versioning and configuration
CREATE TABLE IF NOT EXISTS gt_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Full-text search (IF NOT EXISTS supported in SQLite 3.26+ for virtual tables)
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(name, file_path, signature, documentation);
