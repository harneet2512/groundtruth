/**
 * Import validator unit tests.
 * In-memory SQLite with pre-inserted symbols/exports.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';
import { validateImports } from '../../../src/validators/import-validator.js';

describe('validateImports', () => {
  let store: SymbolStore;

  beforeEach(() => {
    store = new SymbolStore(':memory:');
    store.init();
    const now = Math.floor(Date.now() / 1000);

    // Insert login function in src/auth/login.ts
    const id1 = store.insertSymbol({
      name: 'login',
      kind: 'function',
      file_path: 'src/auth/login.ts',
      line_number: 1,
      is_exported: 1,
      signature: '(email: string, password: string) => Promise<LoginResult>',
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id1, 'src/auth/login', { isDefault: false, isNamed: true });

    // Insert logout function in same module path
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

    // Insert verifyToken as default export
    const id3 = store.insertSymbol({
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
    store.insertExport(id3, 'src/auth/verify', { isDefault: true, isNamed: false });
  });

  it('returns no errors for a valid named import', () => {
    const code = `import { login } from './login';`;
    const errors = validateImports(store, code, 'src/auth/index.ts');
    expect(errors).toHaveLength(0);
  });

  it('returns symbol_not_found for non-existent export from existing module', () => {
    const code = `import { nonExistent } from './login';`;
    const errors = validateImports(store, code, 'src/auth/index.ts');
    expect(errors).toHaveLength(1);
    expect(errors[0].type).toBe('symbol_not_found');
    expect(errors[0].symbol).toBe('nonExistent');
  });

  it('returns module_not_found for non-existent module', () => {
    const code = `import { something } from './missing-module';`;
    const errors = validateImports(store, code, 'src/auth/index.ts');
    expect(errors).toHaveLength(1);
    expect(errors[0].type).toBe('module_not_found');
  });

  it('validates multiple named imports in one statement', () => {
    const code = `import { login, logout } from './login';`;
    const errors = validateImports(store, code, 'src/auth/index.ts');
    expect(errors).toHaveLength(0);
  });

  it('returns error for one bad import among multiple', () => {
    const code = `import { login, badExport } from './login';`;
    const errors = validateImports(store, code, 'src/auth/index.ts');
    expect(errors).toHaveLength(1);
    expect(errors[0].symbol).toBe('badExport');
  });

  it('provides Levenshtein suggestions for close names', () => {
    const code = `import { loginn } from './login';`;
    const errors = validateImports(store, code, 'src/auth/index.ts');
    expect(errors).toHaveLength(1);
    expect(errors[0].type).toBe('symbol_not_found');
    expect(errors[0].suggestions).toBeDefined();
    expect(errors[0].suggestions).toContain('login');
  });

  it('skips non-relative imports (handled by package-validator)', () => {
    const code = `import { something } from 'express';`;
    const errors = validateImports(store, code, 'src/index.ts');
    expect(errors).toHaveLength(0);
  });
});
