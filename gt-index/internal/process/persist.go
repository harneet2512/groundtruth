package process

import (
	"database/sql"
	"fmt"
)

// Schema is the additive DDL for the two tables this package owns. It is
// created here, not in internal/store/sqlite.go, so the analysis layer can be
// added, changed or dropped without touching the protected core-graph schema
// (final_hardening item 9 splits publication along exactly this seam).
//
// The load-bearing line is
//
//	witness_assertion_id INTEGER NOT NULL REFERENCES assertions(id)
//
// NOT NULL is the point of item 8. The acceptance criterion is
//
//	select count(*) from processes where witness_assertion_id is null   -- 0
//
// and with this column definition that query cannot return anything but 0,
// because the database rejects the insert. The invariant is enforced by the
// schema, not merely by the code path that happens to populate it — a second
// writer, a migration, or a future bug cannot publish an unwitnessed process.
//
// process_steps stores the ordered path one row per step rather than as a
// serialized blob, so a step is joinable back to `resolution_symbols` and a
// reader can ask "which processes traverse this symbol" with an index instead
// of a string scan.
const Schema = `
CREATE TABLE IF NOT EXISTS processes (
	id                   TEXT PRIMARY KEY,
	entry_stable_id      TEXT NOT NULL,
	terminal_stable_id   TEXT NOT NULL,
	witness_assertion_id INTEGER NOT NULL REFERENCES assertions(id),
	test_stable_id       TEXT NOT NULL,
	kind                 TEXT NOT NULL DEFAULT '',
	depth                INTEGER NOT NULL,
	trust_floor          TEXT NOT NULL,
	CHECK (depth >= 1),
	CHECK (trust_floor <> '')
);

CREATE TABLE IF NOT EXISTS process_steps (
	process_id TEXT NOT NULL REFERENCES processes(id) ON DELETE CASCADE,
	ordinal    INTEGER NOT NULL,
	stable_id  TEXT NOT NULL,
	PRIMARY KEY(process_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_processes_witness ON processes(witness_assertion_id);
CREATE INDEX IF NOT EXISTS idx_processes_entry ON processes(entry_stable_id);
CREATE INDEX IF NOT EXISTS idx_processes_terminal ON processes(terminal_stable_id);
CREATE INDEX IF NOT EXISTS idx_process_steps_symbol ON process_steps(stable_id);
`

// EnsureSchema creates the process tables if they are absent. It is safe to
// call on every open: every statement is IF NOT EXISTS.
func EnsureSchema(db *sql.DB) error {
	if _, err := db.Exec(Schema); err != nil {
		return fmt.Errorf("create process schema: %w", err)
	}
	return nil
}

// Persist writes a derivation result into the process tables inside the
// caller's transaction, so process publication commits or rolls back with
// whatever else that transaction carries.
//
// The tables are replaced wholesale rather than merged: a derivation is a pure
// function of the graph, so a partial overwrite could leave a process from a
// previous graph state standing next to one from the current state with no way
// to tell them apart.
//
// Persist does not create the schema; call EnsureSchema on the *sql.DB first.
// DDL inside the publication transaction would defeat the point of running the
// DDL once at open time.
func Persist(tx *sql.Tx, result Result) error {
	if _, err := tx.Exec(`DELETE FROM process_steps`); err != nil {
		return fmt.Errorf("clear process_steps: %w", err)
	}
	if _, err := tx.Exec(`DELETE FROM processes`); err != nil {
		return fmt.Errorf("clear processes: %w", err)
	}

	insertProcess, err := tx.Prepare(`
		INSERT INTO processes
			(id, entry_stable_id, terminal_stable_id, witness_assertion_id,
			 test_stable_id, kind, depth, trust_floor)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)`)
	if err != nil {
		return fmt.Errorf("prepare process insert: %w", err)
	}
	defer insertProcess.Close()

	insertStep, err := tx.Prepare(`
		INSERT INTO process_steps (process_id, ordinal, stable_id) VALUES (?, ?, ?)`)
	if err != nil {
		return fmt.Errorf("prepare process step insert: %w", err)
	}
	defer insertStep.Close()

	for _, p := range result.Processes {
		// Belt and braces in front of the NOT NULL column: refuse to attempt
		// the write at all rather than rely on the driver to surface the
		// constraint violation.
		if p.WitnessAssertionID == 0 {
			return fmt.Errorf("process %s has no witnessing assertion", p.ID)
		}
		if len(p.Path) < MinProcessDepth+1 {
			return fmt.Errorf("process %s has a %d-symbol path; minimum is %d",
				p.ID, len(p.Path), MinProcessDepth+1)
		}
		if _, err := insertProcess.Exec(p.ID, p.EntryStableID, p.TerminalStableID,
			p.WitnessAssertionID, p.TestStableID, p.Kind, p.Depth, p.TrustFloor); err != nil {
			return fmt.Errorf("insert process %s: %w", p.ID, err)
		}
		for i, sid := range p.Path {
			if _, err := insertStep.Exec(p.ID, i, sid); err != nil {
				return fmt.Errorf("insert step %d of process %s: %w", i, p.ID, err)
			}
		}
	}
	return nil
}

// Load reads persisted processes back, in the same order Derive emits them.
// It exists so a round-trip is testable and so consumers do not have to
// re-implement the step reassembly.
func Load(db *sql.DB) ([]Process, error) {
	rows, err := db.Query(`
		SELECT id, entry_stable_id, terminal_stable_id, witness_assertion_id,
		       test_stable_id, kind, depth, trust_floor
		FROM processes
		ORDER BY test_stable_id, entry_stable_id, terminal_stable_id, id`)
	if err != nil {
		return nil, fmt.Errorf("read processes: %w", err)
	}
	defer rows.Close()

	var out []Process
	byID := make(map[string]int)
	for rows.Next() {
		var p Process
		if err := rows.Scan(&p.ID, &p.EntryStableID, &p.TerminalStableID,
			&p.WitnessAssertionID, &p.TestStableID, &p.Kind, &p.Depth, &p.TrustFloor); err != nil {
			return nil, fmt.Errorf("scan process: %w", err)
		}
		byID[p.ID] = len(out)
		out = append(out, p)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	steps, err := db.Query(`SELECT process_id, stable_id FROM process_steps ORDER BY process_id, ordinal`)
	if err != nil {
		return nil, fmt.Errorf("read process steps: %w", err)
	}
	defer steps.Close()

	for steps.Next() {
		var pid, sid string
		if err := steps.Scan(&pid, &sid); err != nil {
			return nil, fmt.Errorf("scan process step: %w", err)
		}
		idx, ok := byID[pid]
		if !ok {
			return nil, fmt.Errorf("orphan process step for %s", pid)
		}
		out[idx].Path = append(out[idx].Path, sid)
	}
	if err := steps.Err(); err != nil {
		return nil, err
	}

	sortProcesses(out)
	return out, nil
}
