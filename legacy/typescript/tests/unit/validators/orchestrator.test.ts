/**
 * Orchestrator tests — merges errors from all validators, attaches fixes.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';
import { validate } from '../../../src/validators/index.js';

describe('validate orchestrator', () => {
  let store: SymbolStore;

  beforeEach(() => {
    store = new SymbolStore(':memory:');
    store.init();
    const now = Math.floor(Date.now() / 1000);

    // login in src/auth/login.ts
    const id1 = store.insertSymbol({
      name: 'login',
      kind: 'function',
      file_path: 'src/auth/login.ts',
      line_number: 6,
      is_exported: 1,
      signature: '(email: string, password: string) => Promise<LoginResult>',
      params: JSON.stringify([
        { name: 'email', type: 'string' },
        { name: 'password', type: 'string' },
      ]),
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id1, 'src/auth/login', { isDefault: false, isNamed: true });

    // logout in same module
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

    // validateEmail in src/utils/validation.ts
    const id3 = store.insertSymbol({
      name: 'validateEmail',
      kind: 'function',
      file_path: 'src/utils/validation.ts',
      line_number: 1,
      is_exported: 1,
      signature: '(email: string) => boolean',
      params: JSON.stringify([{ name: 'email', type: 'string' }]),
      return_type: 'boolean',
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id3, 'src/utils/validation', { isDefault: false, isNamed: true });

    // Packages
    store.insertPackage('express', '^4.18.0', false);
  });

  it('returns valid:true for clean code', () => {
    const code = `import { login } from './login';\nlogin('a', 'b');`;
    const result = validate(store, code, 'src/auth/index.ts');
    expect(result.valid).toBe(true);
    expect(result.errors).toHaveLength(0);
  });

  it('merges errors from all three validators', () => {
    const code = [
      `import { nonExistent } from './login';`,
      `import axios from 'axios';`,
      `login('only-one-arg');`,
    ].join('\n');
    const result = validate(store, code, 'src/auth/index.ts');
    expect(result.valid).toBe(false);
    const types = result.errors.map((e) => e.type);
    expect(types).toContain('symbol_not_found');
    expect(types).toContain('package_not_installed');
    expect(types).toContain('signature_mismatch');
  });

  it('attaches Levenshtein fix for close symbol name', () => {
    const code = `import { loginn } from './login';`;
    const result = validate(store, code, 'src/auth/index.ts');
    expect(result.errors).toHaveLength(1);
    expect(result.errors[0].fix).toBeDefined();
    expect(result.errors[0].fix!.source).toBe('levenshtein');
    expect(result.errors[0].fix!.suggestion).toContain('login');
  });

  it('attaches cross-index fix when symbol found in different module', () => {
    // Import validateEmail from wrong module
    const code = `import { validateEmail } from './login';`;
    const result = validate(store, code, 'src/auth/index.ts');
    expect(result.errors).toHaveLength(1);
    expect(result.errors[0].type).toBe('symbol_not_found');
    expect(result.errors[0].fix).toBeDefined();
    expect(result.errors[0].fix!.source).toBe('cross_index');
    expect(result.errors[0].fix!.suggestion).toContain('src/utils/validation');
  });

  it('attaches expected signature for signature_mismatch', () => {
    const code = `login();`;
    const result = validate(store, code, 'src/auth/index.ts');
    const sigError = result.errors.find((e) => e.type === 'signature_mismatch');
    expect(sigError).toBeDefined();
    expect(sigError!.fix).toBeDefined();
    expect(sigError!.fix!.suggestion).toContain('Expected signature');
  });

  it('no fix for package_not_installed', () => {
    const code = `import axios from 'axios';`;
    const result = validate(store, code, 'src/index.ts');
    expect(result.errors).toHaveLength(1);
    expect(result.errors[0].type).toBe('package_not_installed');
    expect(result.errors[0].fix).toBeUndefined();
  });

  it('generates signature fix from params when expectedSignature is null', () => {
    const now = Math.floor(Date.now() / 1000);
    // Add a symbol with params but no signature
    const id = store.insertSymbol({
      name: 'processData',
      kind: 'function',
      file_path: 'src/utils/data.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: JSON.stringify([
        { name: 'input', type: 'string' },
        { name: 'options', type: 'Options', optional: true },
      ]),
      return_type: 'Result',
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id, 'src/utils/data', { isDefault: false, isNamed: true });

    // Code that calls processData with wrong args — triggers signature mismatch
    const code = `processData();`;
    const result = validate(store, code, 'src/utils/index.ts');
    const sigError = result.errors.find(e => e.type === 'signature_mismatch' && e.symbol === 'processData');
    expect(sigError).toBeDefined();
    expect(sigError!.fix).toBeDefined();
    expect(sigError!.fix!.suggestion).toContain('Expected signature');
    expect(sigError!.fix!.suggestion).toContain('input: string');
  });

  it('prefers cross-index over distance-3 Levenshtein suggestion', () => {
    const now = Math.floor(Date.now() / 1000);
    // Add generateHash in a different module (utils/crypto) — exact match for cross-index
    const id = store.insertSymbol({
      name: 'generateHash',
      kind: 'function',
      file_path: 'src/utils/crypto.ts',
      line_number: 1,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id, 'src/utils/crypto', { isDefault: false, isNamed: true });

    // Add generateSalt to login module (distance 3 from generateHash)
    const id2 = store.insertSymbol({
      name: 'generateSalt',
      kind: 'function',
      file_path: 'src/auth/login.ts',
      line_number: 10,
      is_exported: 1,
      signature: null,
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id2, 'src/auth/login', { isDefault: false, isNamed: true });

    // Import generateHash from login — generateSalt is a distance-3 Levenshtein match
    // but generateHash exists in cross-index — should prefer cross-index
    const code = `import { generateHash } from './login';`;
    const result = validate(store, code, 'src/auth/index.ts');
    const notFoundErr = result.errors.find(e => e.type === 'symbol_not_found' && e.symbol === 'generateHash');
    expect(notFoundErr).toBeDefined();
    expect(notFoundErr!.fix).toBeDefined();
    expect(notFoundErr!.fix!.source).toBe('cross_index');
    expect(notFoundErr!.fix!.suggestion).toContain('src/utils/crypto');
  });

  it('assigns high confidence for distance-1 Levenshtein match', () => {
    const code = `import { loginn } from './login';`;
    const result = validate(store, code, 'src/auth/index.ts');
    expect(result.errors).toHaveLength(1);
    expect(result.errors[0].fix).toBeDefined();
    expect(result.errors[0].fix!.source).toBe('levenshtein');
    expect(result.errors[0].fix!.confidence).toBe('high');
  });
});
