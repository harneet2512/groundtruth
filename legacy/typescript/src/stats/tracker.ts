/**
 * Intervention Tracker — Logs every tool call to SQLite.
 *
 * Every groundtruth_generate and groundtruth_validate call gets one row.
 * This powers the `groundtruth stats` CLI, the stats section of
 * groundtruth_status MCP tool, and benchmark data collection.
 *
 * Also handles ai_fix_accepted inference: when validate runs on a file
 * that generate already processed, we check if the errors are gone
 * (fix accepted) or still there (fix rejected).
 */

import Database from "better-sqlite3";

export interface InterventionLog {
  tool: "generate" | "validate";
  file_path: string | null;
  phase: string;
  outcome: "clean" | "caught" | "fixed";
  errors_found: number;
  errors_fixed: number;
  error_types: string[];
  latency_ms: number;
  ai_called: boolean;
  ai_type?: string | null;
  fix_accepted: boolean | null;
}

export interface InterventionRow {
  id: number;
  tool: string;
  file_path: string | null;
  ai_called: boolean;
}

export class InterventionTracker {
  private db: Database.Database;

  constructor(db: Database.Database) {
    this.db = db;
    this.ensureTable();
  }

  private ensureTable(): void {
    // Table created by schema.sql (CLAUDE); ensure index exists
    this.db.exec(`
      CREATE INDEX IF NOT EXISTS idx_interventions_timestamp ON interventions(timestamp);
      CREATE INDEX IF NOT EXISTS idx_interventions_file ON interventions(file_path);
    `);
  }

  log(entry: InterventionLog): void {
    this.db
      .prepare(
        `INSERT INTO interventions
        (timestamp, tool, file_path, phase, outcome, errors_found, errors_fixed, error_types, ai_called, ai_type, latency_ms, fix_accepted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
      )
      .run(
        Math.floor(Date.now() / 1000),
        entry.tool,
        entry.file_path,
        entry.phase,
        entry.outcome,
        entry.errors_found,
        entry.errors_fixed,
        JSON.stringify(entry.error_types),
        entry.ai_called ? 1 : 0,
        entry.ai_type ?? null,
        entry.latency_ms,
        entry.fix_accepted === null ? null : entry.fix_accepted ? 1 : 0
      );
  }

  /**
   * Get the most recent generate call for a file, to infer ai_fix_accepted.
   */
  getLastGenerateForFile(filePath: string): InterventionRow | null {
    return (
      (this.db
        .prepare(
          `SELECT id, tool, file_path, ai_called FROM interventions
         WHERE tool = 'generate' AND file_path = ?
         ORDER BY timestamp DESC LIMIT 1`
        )
        .get(filePath) as InterventionRow | undefined) ?? null
    );
  }

  /**
   * Update fix_accepted for a prior generate call.
   */
  updateAcceptance(id: number, accepted: boolean): void {
    this.db
      .prepare("UPDATE interventions SET fix_accepted = ? WHERE id = ?")
      .run(accepted ? 1 : 0, id);
  }

  /**
   * Get summary stats for the last N days.
   */
  getSummary(days: number): {
    total: number;
    clean: number;
    caught: number;
    fixed: number;
    fixRate: number;
    avgLatencyClean: number;
    avgLatencyFix: number;
    byType: Record<string, number>;
  } {
    const since = new Date();
    since.setDate(since.getDate() - days);
    const sinceSec = Math.floor(since.getTime() / 1000);

    const rows = this.db
      .prepare(
        "SELECT * FROM interventions WHERE timestamp >= ?"
      )
      .all(sinceSec) as Array<{
      outcome: string;
      error_types: string;
      latency_ms: number;
      ai_called: number;
    }>;

    const total = rows.length;
    const clean = rows.filter((r) => r.outcome === "clean").length;
    const caught = rows.filter((r) => r.outcome !== "clean").length;
    const fixed = rows.filter((r) => r.outcome === "fixed").length;

    const cleanLatencies = rows
      .filter((r) => r.outcome === "clean")
      .map((r) => r.latency_ms);
    const fixLatencies = rows
      .filter((r) => r.outcome !== "clean")
      .map((r) => r.latency_ms);

    const byType: Record<string, number> = {};
    for (const row of rows) {
      if (row.error_types) {
        const types = JSON.parse(row.error_types) as string[];
        for (const t of types) {
          byType[t] = (byType[t] ?? 0) + 1;
        }
      }
    }

    return {
      total,
      clean,
      caught,
      fixed,
      fixRate: caught > 0 ? (fixed / caught) * 100 : 0,
      avgLatencyClean: avg(cleanLatencies),
      avgLatencyFix: avg(fixLatencies),
      byType,
    };
  }
}

function avg(nums: number[]): number {
  if (nums.length === 0) return 0;
  return Math.round(nums.reduce((a, b) => a + b, 0) / nums.length);
}
