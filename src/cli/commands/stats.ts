/**
 * CLI: groundtruth stats [--json] [--days N] [--file GLOB]
 * Queries interventions table, outputs formatted stats.
 */
import { openStore } from '../helpers.js';
import { InterventionTracker } from '../../stats/tracker.js';
import { formatStats } from '../../stats/reporter.js';

export async function handleStats(options: {
  json?: boolean;
  days?: string;
  file?: string;
}): Promise<void> {
  const days = parseInt(options.days ?? '7', 10);
  if (isNaN(days) || days <= 0) {
    console.error('Error: --days must be a positive integer');
    process.exitCode = 1;
    return;
  }

  const { store } = openStore({ mustExist: true });
  try {
    const tracker = new InterventionTracker(store.db);
    const output = formatStats(tracker, {
      days,
      json: options.json,
      file: options.file,
    });
    console.log(output);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`Error: ${msg}`);
    process.exitCode = 1;
  } finally {
    store.close();
  }
}
