/**
 * SymbolGraph.getRelevantContext unit tests.
 */
import { describe, it, expect } from 'vitest';
import { mkdtempSync, rmSync, mkdirSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import { SymbolGraph } from '../../../src/symbol-graph/index.js';

describe('SymbolGraph.getRelevantContext', () => {
  it('returns relevant_symbols for intent', () => {
    const tmp = mkdtempSync(join(tmpdir(), 'gt-'));
    mkdirSync(join(tmp, '.groundtruth'), { recursive: true });
    let graph: SymbolGraph;
    try {
      graph = new SymbolGraph(tmp);
      const store = graph.store;
      const now = Math.floor(Date.now() / 1000);
      const id = store.insertSymbol({
        name: 'login',
        kind: 'function',
        file_path: 'src/auth/login.ts',
        line_number: 1,
        is_exported: 1,
        signature: 'login(): void',
        params: null,
        return_type: null,
        jsdoc: null,
        usage_count: 0,
        last_indexed_at: now,
      });
      store.insertExport(id, 'src/auth/login', { isDefault: false, isNamed: true });

      const ctx = graph.getRelevantContext('login');
      expect(ctx.relevant_symbols.length).toBeGreaterThanOrEqual(1);
      expect(ctx.relevant_symbols.some((s) => s.includes('login'))).toBe(true);
    } finally {
      graph.store.close();
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it('returns empty for empty intent', () => {
    const tmp = mkdtempSync(join(tmpdir(), 'gt-'));
    mkdirSync(join(tmp, '.groundtruth'), { recursive: true });
    let graph: SymbolGraph;
    try {
      graph = new SymbolGraph(tmp);
      expect(graph.getRelevantContext('').relevant_symbols).toEqual([]);
    } finally {
      graph!.store.close();
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
