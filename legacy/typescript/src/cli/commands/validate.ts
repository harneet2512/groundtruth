/**
 * CLI: groundtruth validate <file>
 * Reads file from disk, runs validator orchestrator.
 */
import { existsSync, readFileSync } from 'fs';
import { resolve } from 'path';
import { openStore } from '../helpers.js';
import { validate } from '../../validators/index.js';

export async function handleValidate(file: string): Promise<void> {
  const filePath = resolve(process.cwd(), file);
  if (!existsSync(filePath)) {
    console.error(`File not found: ${filePath}`);
    process.exitCode = 1;
    return;
  }

  const { store } = openStore({ mustExist: true });
  try {
    const code = readFileSync(filePath, 'utf-8');
    const result = validate(store, code, filePath);

    if (result.valid) {
      console.log('VALID — no errors found.');
      return;
    }

    console.log(`Found ${result.errors.length} error(s):\n`);
    for (const err of result.errors) {
      console.log(`  [${err.type}] ${err.message}`);
      if (err.fix) {
        console.log(`    Fix: ${err.fix.suggestion}`);
      }
    }
    process.exitCode = 1;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`Error: ${msg}`);
    process.exitCode = 1;
  } finally {
    store.close();
  }
}
