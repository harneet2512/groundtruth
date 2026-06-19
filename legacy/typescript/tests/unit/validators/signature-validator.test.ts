/**
 * Signature validator unit tests.
 * In-memory SQLite with pre-inserted symbols with params.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';
import { validateSignatures } from '../../../src/validators/signature-validator.js';

describe('validateSignatures', () => {
  let store: SymbolStore;

  beforeEach(() => {
    store = new SymbolStore(':memory:');
    store.init();
    const now = Math.floor(Date.now() / 1000);

    // login(email: string, password: string) — 2 required params
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
      return_type: 'Promise<LoginResult>',
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id1, 'src/auth/login', { isDefault: false, isNamed: true });

    // createUser(data: CreateUserInput) — 1 required param
    const id2 = store.insertSymbol({
      name: 'createUser',
      kind: 'function',
      file_path: 'src/users/queries.ts',
      line_number: 5,
      is_exported: 1,
      signature: '(data: CreateUserInput) => Promise<User>',
      params: JSON.stringify([{ name: 'data', type: 'CreateUserInput' }]),
      return_type: 'Promise<User>',
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id2, 'src/users/queries', { isDefault: false, isNamed: true });

    // updateUser(id: number, data: UpdateUserInput) — 2 required params
    const id3 = store.insertSymbol({
      name: 'updateUser',
      kind: 'function',
      file_path: 'src/users/queries.ts',
      line_number: 8,
      is_exported: 1,
      signature: '(id: number, data: UpdateUserInput) => Promise<User>',
      params: JSON.stringify([
        { name: 'id', type: 'number' },
        { name: 'data', type: 'UpdateUserInput' },
      ]),
      return_type: 'Promise<User>',
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });
    store.insertExport(id3, 'src/users/queries', { isDefault: false, isNamed: true });
  });

  it('returns no errors for correct arg count', () => {
    const code = `login('user@example.com', 'pass123');`;
    const errors = validateSignatures(store, code, 'src/index.ts');
    expect(errors).toHaveLength(0);
  });

  it('returns signature_mismatch for too few args', () => {
    const code = `login('user@example.com');`;
    const errors = validateSignatures(store, code, 'src/index.ts');
    expect(errors).toHaveLength(1);
    expect(errors[0].type).toBe('signature_mismatch');
    expect(errors[0].symbol).toBe('login');
    expect(errors[0].message).toContain('2');
    expect(errors[0].message).toContain('1');
  });

  it('returns signature_mismatch for too many args', () => {
    const code = `login('a', 'b', 'c');`;
    const errors = validateSignatures(store, code, 'src/index.ts');
    expect(errors).toHaveLength(1);
    expect(errors[0].type).toBe('signature_mismatch');
    expect(errors[0].symbol).toBe('login');
  });

  it('skips unknown functions (not in index)', () => {
    const code = `unknownFunc(1, 2, 3);`;
    const errors = validateSignatures(store, code, 'src/index.ts');
    expect(errors).toHaveLength(0);
  });

  it('handles optional params correctly', () => {
    // Add a function with an optional param
    const now = Math.floor(Date.now() / 1000);
    store.insertSymbol({
      name: 'greet',
      kind: 'function',
      file_path: 'src/utils/greet.ts',
      line_number: 1,
      is_exported: 1,
      signature: '(name: string, greeting?: string) => string',
      params: JSON.stringify([
        { name: 'name', type: 'string' },
        { name: 'greeting', type: 'string', optional: true },
      ]),
      return_type: 'string',
      jsdoc: null,
      usage_count: 0,
      last_indexed_at: now,
    });

    // 1 arg (only required) — OK
    const code1 = `greet('Alice');`;
    expect(validateSignatures(store, code1, 'src/index.ts')).toHaveLength(0);

    // 2 args (required + optional) — OK
    const code2 = `greet('Alice', 'Hi');`;
    expect(validateSignatures(store, code2, 'src/index.ts')).toHaveLength(0);

    // 0 args — error
    const code3 = `greet();`;
    const errors = validateSignatures(store, code3, 'src/index.ts');
    expect(errors).toHaveLength(1);
    expect(errors[0].type).toBe('signature_mismatch');
  });

  it('handles nested function calls in args', () => {
    const code = `login(getEmail(), getPassword());`;
    const errors = validateSignatures(store, code, 'src/index.ts');
    expect(errors).toHaveLength(0);
  });
});
