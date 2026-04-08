/**
 * Indexer unit tests: indexProject on test-project fixture.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { join } from 'path';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';
import { ProjectIndexer } from '../../../src/symbol-graph/indexer.js';

const FIXTURE = join(process.cwd(), 'tests', 'fixtures', 'test-project');

describe('ProjectIndexer', () => {
  let store: SymbolStore;
  let indexer: ProjectIndexer;

  beforeEach(() => {
    store = new SymbolStore(':memory:');
    store.init();
    indexer = new ProjectIndexer(store);
  });

  it('indexes test-project and returns non-zero stats', async () => {
    const stats = await indexer.indexProject(FIXTURE);
    expect(stats.totalSymbols).toBeGreaterThan(0);
    expect(stats.totalFiles).toBeGreaterThan(0);
    expect(stats.totalPackages).toBeGreaterThan(0);
  });

  it('indexes auth-related symbols (login, logout, verifyToken, signToken)', async () => {
    await indexer.indexProject(FIXTURE);
    const loginRows = store.getSymbolByName('login');
    const logoutRows = store.getSymbolByName('logout');
    const verifyRows = store.getSymbolByName('verifyToken');
    const signRows = store.getSymbolByName('signToken');
    expect(loginRows.length).toBeGreaterThanOrEqual(1);
    expect(logoutRows.length).toBeGreaterThanOrEqual(1);
    expect(verifyRows.length).toBeGreaterThanOrEqual(1);
    expect(signRows.length).toBeGreaterThanOrEqual(1);
  });

  it('indexes hashPassword in utils/crypto', async () => {
    await indexer.indexProject(FIXTURE);
    const rows = store.getSymbolByName('hashPassword');
    expect(rows.length).toBeGreaterThanOrEqual(1);
    expect(rows.some((r) => r.file_path.includes('crypto'))).toBe(true);
  });

  it('indexes packages from package.json (express, zod, bcrypt)', async () => {
    await indexer.indexProject(FIXTURE);
    expect(store.getPackage('express')).not.toBeNull();
    expect(store.getPackage('zod')).not.toBeNull();
    expect(store.getPackage('bcrypt')).not.toBeNull();
  });

  it('does not list axios as installed', async () => {
    await indexer.indexProject(FIXTURE);
    expect(store.getPackage('axios')).toBeNull();
  });

  it('populates signature for functions with params (buildSignatureString fallback)', async () => {
    await indexer.indexProject(FIXTURE);
    const rows = store.getExportedSymbolsByName('hashPassword');
    expect(rows.length).toBeGreaterThanOrEqual(1);
    // Either ts-morph getSignature() or buildSignatureString() should produce a non-null signature
    const hasSignature = rows.some(r => r.signature !== null);
    expect(hasSignature).toBe(true);
  });

  it('records import references: middleware/auth imports verifyToken', async () => {
    await indexer.indexProject(FIXTURE);
    const verifyRows = store.getSymbolByName('verifyToken');
    const authVerify = verifyRows.find((r) => r.file_path.replace(/\\/g, '/').includes('auth/verify'));
    expect(authVerify).toBeDefined();
    const symbolId = authVerify!.symbol_id;
    const refs = store.getReferences(symbolId);
    const fromAuth = refs.filter((r) => r.referenced_in_file.replace(/\\/g, '/').includes('middleware/auth'));
    expect(fromAuth.length).toBeGreaterThanOrEqual(1);
    expect(fromAuth.some((r) => r.reference_type === 'import')).toBe(true);
  });

  it('records import references: errorHandler imports AppError', async () => {
    await indexer.indexProject(FIXTURE);
    const appErrorRows = store.getSymbolByName('AppError');
    const utilsErrors = appErrorRows.find((r) => r.file_path.replace(/\\/g, '/').includes('utils/errors'));
    expect(utilsErrors).toBeDefined();
    const symbolId = utilsErrors!.symbol_id;
    const refs = store.getReferences(symbolId);
    const fromErrorHandler = refs.filter((r) => r.referenced_in_file.replace(/\\/g, '/').includes('errorHandler'));
    expect(fromErrorHandler.length).toBeGreaterThanOrEqual(1);
    expect(fromErrorHandler.some((r) => r.reference_type === 'import')).toBe(true);
  });

  it('getImportsForFile returns imports for middleware auth', async () => {
    await indexer.indexProject(FIXTURE);
    const authPath = 'src/middleware/auth.ts';
    const imports = store.getImportsForFile(authPath);
    expect(imports.length).toBeGreaterThanOrEqual(1);
    const hasVerify = imports.some((i) => i.symbol_name === 'verifyToken');
    expect(hasVerify).toBe(true);
  });
});
