-- Verification schema — stores patch evaluation results.

CREATE TABLE IF NOT EXISTS patch_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_ref TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    contract_score REAL,
    test_score REAL,
    maintainability_score REAL,
    overall_score REAL,
    decision TEXT NOT NULL,            -- accept | reject | abstain
    explanation_json TEXT,
    evaluated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evaluations_task ON patch_evaluations(task_ref);
CREATE INDEX IF NOT EXISTS idx_evaluations_decision ON patch_evaluations(decision);

CREATE TABLE IF NOT EXISTS contract_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL REFERENCES patch_evaluations(id),
    contract_id INTEGER NOT NULL,
    status TEXT NOT NULL,              -- pass | fail | unknown
    details_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_checks_evaluation ON contract_checks(evaluation_id);
