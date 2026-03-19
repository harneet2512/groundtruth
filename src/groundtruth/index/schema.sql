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

-- Module coverage for index completeness tracking
CREATE TABLE IF NOT EXISTS module_coverage (
    module_path TEXT PRIMARY KEY,
    symbol_count INTEGER NOT NULL DEFAULT 0,
    has_star_import BOOLEAN DEFAULT FALSE,
    has_dynamic_all BOOLEAN DEFAULT FALSE,
    has_dynamic_getattr BOOLEAN DEFAULT FALSE,
    indexed_at INTEGER NOT NULL
);

-- Class attributes (self.* per class)
CREATE TABLE IF NOT EXISTS attributes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    method_ids TEXT  -- JSON array of method symbol_ids that reference this attr
);
CREATE INDEX IF NOT EXISTS idx_attributes_symbol ON attributes(symbol_id);
CREATE INDEX IF NOT EXISTS idx_attributes_name ON attributes(name);

-- Hallucination correction log (the learning layer)
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    hallucinated_name TEXT NOT NULL,
    corrected_to TEXT NOT NULL,
    file TEXT,
    context TEXT,
    check_type TEXT,
    confidence REAL,
    agent_id TEXT,
    timestamp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_repo_name ON corrections(repo, hallucinated_name);
CREATE INDEX IF NOT EXISTS idx_corrections_timestamp ON corrections(timestamp);

-- Activity log for CityView live updates
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    tool TEXT NOT NULL,
    symbol TEXT,
    file TEXT,
    agent_id TEXT,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity(timestamp);

-- Certainty-layered facts (semantic graph)
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,       -- 'class' | 'function' | 'module' | 'variable'
    subject_name TEXT NOT NULL,
    relation TEXT NOT NULL,           -- 'has_member' | 'has_param' | 'exports' | 'imports'
    object_type TEXT NOT NULL,        -- 'method' | 'attribute' | 'param' | 'symbol'
    object_name TEXT NOT NULL,
    provenance TEXT NOT NULL,         -- 'ast' | 'pyright' | 'lsp' | 'introspection'
    certainty TEXT NOT NULL,          -- 'green' | 'yellow' | 'red'
    scope TEXT NOT NULL DEFAULT 'repo_base',  -- 'repo_base' | 'patch' | 'stdlib'
    file_path TEXT,
    line_number INTEGER,
    extra_json TEXT                   -- JSON blob for additional metadata
);
CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject_name);
CREATE INDEX IF NOT EXISTS idx_facts_object ON facts(object_name);
CREATE INDEX IF NOT EXISTS idx_facts_certainty ON facts(certainty);
CREATE INDEX IF NOT EXISTS idx_facts_file ON facts(file_path);

-- Full-text search (IF NOT EXISTS supported in SQLite 3.26+ for virtual tables)
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(name, file_path, signature, documentation);
