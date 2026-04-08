/**
 * Package validator unit tests.
 * In-memory SQLite with pre-inserted packages.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';
import { validatePackages } from '../../../src/validators/package-validator.js';

describe('validatePackages', () => {
  let store: SymbolStore;

  beforeEach(() => {
    store = new SymbolStore(':memory:');
    store.init();
    store.insertPackage('express', '^4.18.0', false);
    store.insertPackage('zod', '^3.23.0', false);
    store.insertPackage('@anthropic-ai/sdk', '^0.30.0', false);
  });

  it('returns no errors for installed package', () => {
    const code = `import express from 'express';`;
    const errors = validatePackages(store, code, 'src/index.ts');
    expect(errors).toHaveLength(0);
  });

  it('returns package_not_installed for missing package', () => {
    const code = `import axios from 'axios';`;
    const errors = validatePackages(store, code, 'src/index.ts');
    expect(errors).toHaveLength(1);
    expect(errors[0].type).toBe('package_not_installed');
    expect(errors[0].symbol).toBe('axios');
  });

  it('handles scoped packages correctly', () => {
    const code = `import { Anthropic } from '@anthropic-ai/sdk';`;
    const errors = validatePackages(store, code, 'src/index.ts');
    expect(errors).toHaveLength(0);
  });

  it('returns error for missing scoped package', () => {
    const code = `import { something } from '@some/missing-pkg';`;
    const errors = validatePackages(store, code, 'src/index.ts');
    expect(errors).toHaveLength(1);
    expect(errors[0].symbol).toBe('@some/missing-pkg');
  });

  it('skips Node.js built-ins (fs, path, etc.)', () => {
    const code = `import { readFileSync } from 'fs';\nimport { join } from 'path';`;
    const errors = validatePackages(store, code, 'src/index.ts');
    expect(errors).toHaveLength(0);
  });

  it('skips node: protocol imports', () => {
    const code = `import { readFile } from 'node:fs/promises';`;
    const errors = validatePackages(store, code, 'src/index.ts');
    expect(errors).toHaveLength(0);
  });

  it('skips relative imports', () => {
    const code = `import { login } from './auth/login';`;
    const errors = validatePackages(store, code, 'src/index.ts');
    expect(errors).toHaveLength(0);
  });

  it('handles sub-path imports (express/Router)', () => {
    const code = `import { Router } from 'express/router';`;
    const errors = validatePackages(store, code, 'src/index.ts');
    expect(errors).toHaveLength(0);
  });
});
