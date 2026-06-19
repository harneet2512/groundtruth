/**
 * CLI: groundtruth stats [--json] [--days N] [--since DATE] [--file GLOB]
 *
 * Detailed intervention statistics. Queries the interventions table
 * and formats a report showing:
 * - Total validations, clean vs caught
 * - AI fix rate
 * - Error type breakdown
 * - Estimated time saved
 * - Latency averages
 */

import Database from "better-sqlite3";
import { InterventionTracker } from "../stats/tracker.js";
import { formatStats } from "../stats/reporter.js";
import { existsSync } from "fs";
import { join } from "path";

export async function handleStats(options: {
  json?: boolean;
  days?: string;
  since?: string;
  file?: string;
}): Promise<void> {
  const dbPath = join(process.cwd(), ".groundtruth", "graph.sqlite");

  if (!existsSync(dbPath)) {
    console.log("No GroundTruth data found. Run groundtruth in a project first.");
    process.exit(1);
  }

  const db = new Database(dbPath, { readonly: true });
  const tracker = new InterventionTracker(db);

  const days = options.days ? parseInt(options.days, 10) : 7;
  const output = formatStats(tracker, { days, json: options.json });

  console.log(output);
  db.close();
}
