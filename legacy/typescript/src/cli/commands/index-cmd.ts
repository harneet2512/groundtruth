/**
 * CLI: groundtruth index
 * Runs project indexer, populates symbol store.
 */
import { openStore } from '../helpers.js';
import { ProjectIndexer } from '../../symbol-graph/indexer.js';

export async function handleIndex(): Promise<void> {
  const { store, dbPath, projectRoot } = openStore();
  try {
    console.log('Indexing project...');
    const indexer = new ProjectIndexer(store);
    const stats = await indexer.indexProject(projectRoot);
    console.log(`  Files:    ${stats.totalFiles}`);
    console.log(`  Symbols:  ${stats.totalSymbols}`);
    console.log(`  Packages: ${stats.totalPackages}`);
    console.log(`  DB:       ${dbPath}`);
    console.log('Done.');
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`Error: ${msg}`);
    process.exitCode = 1;
  } finally {
    store.close();
  }
}
