/**
 * Query layer unit tests: resolveImport, searchSymbolByName, searchSymbolsSemantic.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';
import { resolveImport, searchSymbolByName, searchSymbolsSemantic } from '../../../src/symbol-graph/query.js';

describe('query', () => {
  let store: SymbolStore;

  beforeEach(() => {
    store = new SymbolStore(':memory:');
    store.init();
    const now = Math.floor(Date.now() / 1000);
    const id1 = store.insertSymbol({
      name: 'login',
      kind: 'function',
      file_path: 'src/auth/login.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id1, 'src/auth/login', { isDefault: false, isNamed: true });
    const id2 = store.insertSymbol({
      name: 'logout',
      kind: 'function',
      file_path: 'src/auth/logout.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id2, 'src/auth/login', { isDefault: false, isNamed: true });
  });

  describe('resolveImport', () => {
    it('finds existing export', () => {
      const result = resolveImport(store, './login', 'login', 'src/auth/index.ts');
      expect(result.found).toBe(true);
    });

    it('returns available and suggestions when name wrong', () => {
      const result = resolveImport(store, './login', 'loggin', 'src/auth/index.ts');
      expect(result.found).toBe(false);
      expect(result.available).toContain('login');
      expect(result.available).toContain('logout');
      expect(result.suggestions).toContain('login');
    });

    it('returns bestDistance for Levenshtein suggestions', () => {
      const result = resolveImport(store, './login', 'loggin', 'src/auth/index.ts');
      expect(result.found).toBe(false);
      expect(result.bestDistance).toBeDefined();
      expect(result.bestDistance).toBeGreaterThan(0);
    });

    it('bestDistance is undefined when no suggestions', () => {
      const result = resolveImport(store, './login', 'completelyWrongName', 'src/auth/index.ts');
      expect(result.found).toBe(false);
      expect(result.bestDistance).toBeUndefined();
    });
  });

  describe('searchSymbolByName', () => {
    it('returns file_path and module_path', () => {
      const rows = searchSymbolByName(store, 'login');
      expect(rows.length).toBeGreaterThanOrEqual(1);
      expect(rows[0].file_path).toBe('src/auth/login.ts');
      expect(rows[0].module_path).toBe('src/auth/login');
    });
  });

  describe('searchSymbolsSemantic', () => {
    it('returns rows from FTS5', () => {
      const rows = searchSymbolsSemantic(store, 'login');
      expect(rows.length).toBeGreaterThanOrEqual(1);
      expect(rows[0]).toHaveProperty('name');
      expect(rows[0]).toHaveProperty('kind');
      expect(rows[0]).toHaveProperty('file_path');
      expect(rows[0]).toHaveProperty('usage_count');
    });
  });
});
