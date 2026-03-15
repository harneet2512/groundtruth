-- GroundTruth SQLite Schema (CLAUDE.md)
-- Symbol graph + intervention tracking.

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line_number INTEGER,
    is_exported BOOLEAN DEFAULT FALSE,
    signature TEXT,
    params TEXT,
    return_type TEXT,
    jsdoc TEXT,
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
    name TEXT NOT NULL UNIQUE,
    version TEXT,
    is_dev_dependency BOOLEAN DEFAULT FALSE
);

-- Track where each symbol is referenced (call sites, imports, type usage)
CREATE TABLE IF NOT EXISTS "references" (
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
    phase TEXT NOT NULL,
    outcome TEXT NOT NULL,
    errors_found INTEGER DEFAULT 0,
    errors_fixed INTEGER DEFAULT 0,
    error_types TEXT,
    ai_called BOOLEAN DEFAULT FALSE,
    ai_type TEXT,
    latency_ms INTEGER,
    fix_accepted BOOLEAN
);

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_name_exported ON symbols(name) WHERE is_exported = TRUE;
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_usage ON symbols(usage_count DESC);
CREATE INDEX IF NOT EXISTS idx_exports_module ON exports(module_path);
CREATE INDEX IF NOT EXISTS idx_packages_name ON packages(name);
CREATE INDEX IF NOT EXISTS idx_references_symbol ON "references"(symbol_id);
CREATE INDEX IF NOT EXISTS idx_references_file ON "references"(referenced_in_file);
CREATE INDEX IF NOT EXISTS idx_interventions_timestamp ON interventions(timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(name, file_path, signature, jsdoc);
