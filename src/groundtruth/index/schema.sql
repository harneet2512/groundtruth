CREATE TABLE symbols (
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

CREATE TABLE exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    module_path TEXT NOT NULL,
    is_default BOOLEAN DEFAULT FALSE,
    is_named BOOLEAN DEFAULT TRUE
);

CREATE TABLE packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version TEXT,
    package_manager TEXT NOT NULL,
    is_dev_dependency BOOLEAN DEFAULT FALSE,
    UNIQUE(name, package_manager)
);

CREATE TABLE refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    referenced_in_file TEXT NOT NULL,
    referenced_at_line INTEGER,
    reference_type TEXT NOT NULL
);

CREATE TABLE interventions (
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
    fix_accepted BOOLEAN
);

-- Indexes
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_name_exported ON symbols(name) WHERE is_exported = TRUE;
CREATE INDEX idx_symbols_file ON symbols(file_path);
CREATE INDEX idx_symbols_language ON symbols(language);
CREATE INDEX idx_symbols_usage ON symbols(usage_count DESC);
CREATE INDEX idx_exports_module ON exports(module_path);
CREATE INDEX idx_packages_name ON packages(name);
CREATE INDEX idx_refs_symbol ON refs(symbol_id);
CREATE INDEX idx_refs_file ON refs(referenced_in_file);
CREATE INDEX idx_interventions_timestamp ON interventions(timestamp);

-- Briefing logs for grounding gap analysis
CREATE TABLE briefing_logs (
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
CREATE INDEX idx_briefing_logs_timestamp ON briefing_logs(timestamp);
CREATE INDEX idx_briefing_logs_target ON briefing_logs(target_file);

-- Persistent index metadata for incremental re-indexing
CREATE TABLE IF NOT EXISTS index_metadata (
    file_path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    symbol_count INTEGER NOT NULL,
    indexed_at INTEGER NOT NULL
);

-- Full-text search
CREATE VIRTUAL TABLE symbols_fts USING fts5(name, file_path, signature, documentation);
