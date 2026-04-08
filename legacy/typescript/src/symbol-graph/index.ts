/**
 * Symbol Graph — Index of every exported symbol (CLAUDE spec).
 * Uses SymbolStore (SQLite) + ProjectIndexer (ts-morph). No LSP.
 */
import type Database from 'better-sqlite3';
import { SymbolStore } from './sqlite-store.js';

export class SymbolGraph {
  readonly store: SymbolStore;

  constructor(projectRoot: string) {
    this.store = new SymbolStore(`${projectRoot}/.groundtruth/graph.sqlite`);
    this.store.init();
  }

  get db(): Database.Database {
    return this.store.db;
  }

  get symbolCount(): number {
    return this.store.getStats().symbolCount;
  }

  getRelevantContext(intent: string): {
    relevant_symbols: string[];
    usage_pattern?: string;
  } {
    if (!intent) return { relevant_symbols: [] };
    const symbols = this.store.searchSymbols(intent);
    const relevant_symbols = symbols.map(
      (s) => (s.signature ? `${s.name}: ${s.signature} (${s.file_path})` : `${s.name} (${s.file_path})`)
    );
    return { relevant_symbols };
  }
}

export { SymbolStore } from './sqlite-store.js';
export { ProjectIndexer } from './indexer.js';
export * from './query.js';
export * from './graph-traversal.js';
