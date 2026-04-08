/**
 * CLI: groundtruth status
 * Shows indexed symbol count + intervention stats summary.
 */
import { openStore } from '../helpers.js';
import { InterventionTracker } from '../../stats/tracker.js';

export async function handleStatus(): Promise<void> {
  const { store, dbPath } = openStore({ mustExist: true });
  try {
    const stats = store.getStats();
    const tracker = new InterventionTracker(store.db);
    const summary = tracker.getSummary(7);

    console.log('');
    console.log('  GroundTruth Status');
    console.log('  ' + '─'.repeat(35));
    console.log(`  Database:       ${dbPath}`);
    console.log(`  Symbols:        ${stats.symbolCount}`);
    console.log(`  Packages:       ${stats.packageCount}`);
    console.log(`  Interventions:  ${stats.interventionCount}`);
    console.log('');
    console.log('  Last 7 days:');
    console.log(`    Validations:    ${summary.total}`);
    console.log(`    Clean:          ${summary.clean}`);
    console.log(`    Caught:         ${summary.caught}`);
    console.log(`    Fixed:          ${summary.fixed}`);
    console.log('');
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`Error: ${msg}`);
    process.exitCode = 1;
  } finally {
    store.close();
  }
}
