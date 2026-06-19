/**
 * Shared CLI helpers — store opener for command handlers.
 */
import { existsSync, mkdirSync } from 'fs';
import { join, dirname, resolve } from 'path';
import { SymbolStore } from '../symbol-graph/sqlite-store.js';
import { defaultConfig } from '../config/defaults.js';

export interface StoreContext {
  store: SymbolStore;
  dbPath: string;
  projectRoot: string;
}

export function openStore(opts?: { mustExist?: boolean }): StoreContext {
  const projectRoot = process.cwd();
  const dbPath = resolve(projectRoot, defaultConfig.dbPath);
  const dbDir = dirname(dbPath);

  if (opts?.mustExist && !existsSync(dbPath)) {
    throw new Error('No GroundTruth index found. Run `groundtruth index` first.');
  }

  if (!existsSync(dbDir)) {
    mkdirSync(dbDir, { recursive: true });
  }

  const store = new SymbolStore(dbPath);
  store.init();

  return { store, dbPath, projectRoot };
}
