/**
 * SymbolStore unit tests (CRUD, search, interventions).
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';

describe('SymbolStore', () => {
  let store: SymbolStore;

  beforeEach(() => {
    store = new SymbolStore(':memory:');
    store.init();
  });

  it('creates tables on init and getStats returns zero', () => {
    const stats = store.getStats();
    expect(stats.symbolCount).toBe(0);
    expect(stats.interventionCount).toBe(0);
  });

  it('insertSymbol and getExportsByModule return inserted data', () => {
    const now = Math.floor(Date.now() / 1000);
    const id1 = store.insertSymbol({
      name: 'login',
      kind: 'function',
      file_path: 'src/auth/login.ts',
      line_number: 5,
      is_exported: 1,
      signature: 'login(email: string, password: string): Promise<LoginResult>',
      params: null,
      return_type: 'Promise<LoginResult>',
      jsdoc: null,
      usage_count: 2,
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
      usage_count: 1,
      last_indexed_at: now,
    });
    store.insertExport(id2, 'src/auth/logout', { isDefault: false, isNamed: true });

    const authLoginExports = store.getExportsByModule('src/auth/login');
    expect(authLoginExports).toHaveLength(1);
    expect(authLoginExports[0].name).toBe('login');
    expect(authLoginExports[0].signature).toContain('email');
  });

  it('getSymbolByName returns file_path and module_path', () => {
    const now = Math.floor(Date.now() / 1000);
    const id = store.insertSymbol({
      name: 'verifyToken',
      kind: 'function',
      file_path: 'src/auth/verify.ts',
      line_number: 10,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 5,
      last_indexed_at: now,
    });
    store.insertExport(id, 'src/auth/verify', { isDefault: false, isNamed: true });

    const rows = store.getSymbolByName('verifyToken');
    expect(rows).toHaveLength(1);
    expect(rows[0].file_path).toBe('src/auth/verify.ts');
    expect(rows[0].module_path).toBe('src/auth/verify');
  });

  it('searchSymbols returns relevant results for a keyword', () => {
    const now = Math.floor(Date.now() / 1000);
    const id = store.insertSymbol({
      name: 'login',
      kind: 'function',
      file_path: 'src/auth/login.ts',
      line_number: 1,
      is_exported: 1,
      signature: 'auth login',
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id, 'src/auth/login', { isDefault: false, isNamed: true });

    const results = store.searchSymbols('auth');
    expect(results.length).toBeGreaterThanOrEqual(1);
    expect(results.some((r) => r.name === 'login')).toBe(true);
  });

  it('getSymbolsByUsage orders by usage_count desc', () => {
    const now = Math.floor(Date.now() / 1000);
    const id1 = store.insertSymbol({
      name: 'low',
      kind: 'function',
      file_path: 'src/a.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 1,
      last_indexed_at: now,
    });
    store.insertExport(id1, 'src/a', { isDefault: false, isNamed: true });
    const id2 = store.insertSymbol({
      name: 'high',
      kind: 'function',
      file_path: 'src/b.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 10,
      last_indexed_at: now,
    });
    store.insertExport(id2, 'src/b', { isDefault: false, isNamed: true });

    const rows = store.getSymbolsByUsage(0);
    expect(rows.length).toBe(2);
    expect(rows[0].usage_count).toBe(10);
    expect(rows[0].name).toBe('high');
  });

  it('insertReference and getReferences return reference data', () => {
    const now = Math.floor(Date.now() / 1000);
    const id = store.insertSymbol({
      name: 'verifyToken',
      kind: 'function',
      file_path: 'src/auth/verify.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id, 'src/auth/verify', { isDefault: false, isNamed: true });
    store.insertReference(id, 'src/middleware/auth.ts', 3, 'import');
    store.insertReference(id, 'src/routes/auth.ts', 10, 'call');
    const refs = store.getReferences(id);
    expect(refs).toHaveLength(2);
    expect(refs.map((r) => r.referenced_in_file).sort()).toEqual(['src/middleware/auth.ts', 'src/routes/auth.ts']);
    expect(refs.find((r) => r.reference_type === 'import')?.referenced_at_line).toBe(3);
  });

  it('getImportsForFile returns symbols imported by the file', () => {
    const now = Math.floor(Date.now() / 1000);
    const id = store.insertSymbol({
      name: 'AppError',
      kind: 'class',
      file_path: 'src/utils/errors.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id, 'src/utils/errors', { isDefault: false, isNamed: true });
    store.insertReference(id, 'src/middleware/errorHandler.ts', 2, 'import');
    const imports = store.getImportsForFile('src/middleware/errorHandler.ts');
    expect(imports).toHaveLength(1);
    expect(imports[0].symbol_name).toBe('AppError');
    expect(imports[0].symbol_file_path).toBe('src/utils/errors.ts');
  });

  it('getImportersOfFile returns files that import from the given file', () => {
    const now = Math.floor(Date.now() / 1000);
    const id = store.insertSymbol({
      name: 'verifyToken',
      kind: 'function',
      file_path: 'src/auth/verify.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id, 'src/auth/verify', { isDefault: false, isNamed: true });
    store.insertReference(id, 'src/middleware/auth.ts', 1, 'import');
    const importers = store.getImportersOfFile('src/auth/verify.ts');
    expect(importers).toContain('src/middleware/auth.ts');
  });

  it('getDefaultExportIdForModule returns symbol_id for default export', () => {
    const now = Math.floor(Date.now() / 1000);
    const id = store.insertSymbol({
      name: 'default',
      kind: 'variable',
      file_path: 'src/thing.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id, 'src/thing', { isDefault: true, isNamed: false });
    expect(store.getDefaultExportIdForModule('src/thing')).toBe(id);
    expect(store.getDefaultExportIdForModule('src/nonexistent')).toBeNull();
  });

  it('clearFile removes symbols and references for that file', () => {
    const now = Math.floor(Date.now() / 1000);
    const id = store.insertSymbol({
      name: 'foo',
      kind: 'function',
      file_path: 'src/toRemove.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id, 'src/toRemove', { isDefault: false, isNamed: true });
    store.insertReference(id, 'src/other.ts', 1, 'import');
    expect(store.getReferences(id)).toHaveLength(1);
    store.clearFile('src/toRemove.ts');
    expect(store.getStats().symbolCount).toBe(0);
    expect(store.getExportsByModule('src/toRemove')).toHaveLength(0);
    expect(store.getReferences(id)).toHaveLength(0);
  });

  it('clearFile removes references where referenced_in_file is the cleared file', () => {
    const now = Math.floor(Date.now() / 1000);
    const id = store.insertSymbol({
      name: 'bar',
      kind: 'function',
      file_path: 'src/keeper.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id, 'src/keeper', { isDefault: false, isNamed: true });
    store.insertReference(id, 'src/consumer.ts', 5, 'import');
    store.clearFile('src/consumer.ts');
    expect(store.getImportsForFile('src/consumer.ts')).toHaveLength(0);
    expect(store.getReferences(id)).toHaveLength(0);
  });

  it('clearFile removes symbols and exports for that file', () => {
    const now = Math.floor(Date.now() / 1000);
    const id = store.insertSymbol({
      name: 'foo',
      kind: 'function',
      file_path: 'src/toRemove.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id, 'src/toRemove', { isDefault: false, isNamed: true });
    expect(store.getStats().symbolCount).toBe(1);
    store.clearFile('src/toRemove.ts');
    expect(store.getStats().symbolCount).toBe(0);
    expect(store.getExportsByModule('src/toRemove')).toHaveLength(0);
  });

  it('logIntervention increments interventionCount in getStats', () => {
    expect(store.getStats().interventionCount).toBe(0);
    store.logIntervention({
      tool: 'generate',
      file_path: 'src/foo.ts',
      phase: 'generate',
      outcome: 'caught',
      errors_found: 1,
      errors_fixed: 0,
      error_types: 'symbol_not_found',
      ai_called: false,
      ai_type: null,
      latency_ms: 10,
      fix_accepted: null,
    });
    expect(store.getStats().interventionCount).toBe(1);
  });
});
