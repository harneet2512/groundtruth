-- Contract Engine schema — extends graph.db with behavioral contracts.
--
-- These tables are additive (CREATE IF NOT EXISTS) so existing graph.db
-- files continue to work. The contract engine creates them on first run.

-- Behavioral contracts extracted from evidence
CREATE TABLE IF NOT EXISTS contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_type TEXT NOT NULL,        -- exception_message | exact_output | roundtrip
    scope_kind TEXT NOT NULL,           -- function | method | class | module
    scope_ref TEXT NOT NULL,            -- qualified name (e.g. 'mymod.MyClass.method')
    node_id INTEGER,                    -- FK to nodes.id (nullable for cross-DB use)
    predicate TEXT NOT NULL,            -- human-readable description
    normalized_form TEXT NOT NULL,      -- machine-comparable canonical form
    support_count INTEGER NOT NULL,     -- number of independent sources
    confidence REAL NOT NULL,           -- 0.0-1.0
    tier TEXT NOT NULL,                 -- verified | likely | possible
    extracted_at INTEGER NOT NULL,      -- unix timestamp
    UNIQUE(contract_type, scope_ref, normalized_form)
);

CREATE INDEX IF NOT EXISTS idx_contracts_scope ON contracts(scope_ref);
CREATE INDEX IF NOT EXISTS idx_contracts_type ON contracts(contract_type);
CREATE INDEX IF NOT EXISTS idx_contracts_tier ON contracts(tier);
CREATE INDEX IF NOT EXISTS idx_contracts_node ON contracts(node_id);

-- Evidence supporting each contract (provenance tracking)
CREATE TABLE IF NOT EXISTS contract_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    source_file TEXT NOT NULL,          -- file containing this evidence
    source_line INTEGER,                -- line number
    source_kind TEXT NOT NULL,          -- test_assertion | guard_clause | caller_catch | type_annotation | caller_destructure
    detail TEXT,                        -- raw assertion text or property value
    confidence REAL NOT NULL            -- per-source confidence
);

CREATE INDEX IF NOT EXISTS idx_contract_evidence_contract ON contract_evidence(contract_id);
CREATE INDEX IF NOT EXISTS idx_contract_evidence_kind ON contract_evidence(source_kind);
