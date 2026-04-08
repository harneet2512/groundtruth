/**
 * GraphTraversal unit tests: findConnectedFiles, findCallers, findCallees, getImpactRadius.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { join } from 'path';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';
import { ProjectIndexer } from '../../../src/symbol-graph/indexer.js';
import { GraphTraversal } from '../../../src/symbol-graph/graph-traversal.js';

const FIXTURE = join(process.cwd(), 'tests', 'fixtures', 'test-project');

describe('GraphTraversal', () => {
  let store: SymbolStore;
  let traversal: GraphTraversal;

  beforeEach(async () => {
    store = new SymbolStore(':memory:');
    store.init();
    const indexer = new ProjectIndexer(store);
    await indexer.indexProject(FIXTURE);
    traversal = new GraphTraversal(store);
  });

  it('findConnectedFiles from users/queries.ts includes users/types and db/client if referenced', () => {
    const entry = 'src/users/queries.ts';
    const connected = traversal.findConnectedFiles([entry], 2);
    const paths = connected.map((n) => n.path.replace(/\\/g, '/'));
    expect(paths).toContain(entry.replace(/\\/g, '/'));
    expect(paths.some((p) => p.includes('users/types'))).toBe(true);
  });

  it('findCallers(verifyToken) returns middleware/auth', () => {
    const refs = traversal.findCallers('verifyToken');
    const files = refs.map((r) => r.referenced_in_file.replace(/\\/g, '/'));
    expect(files.some((f) => f.includes('middleware/auth'))).toBe(true);
  });

  it('findCallers(AppError) returns errorHandler file', () => {
    const refs = traversal.findCallers('AppError');
    const files = refs.map((r) => r.referenced_in_file.replace(/\\/g, '/'));
    expect(files.some((f) => f.includes('errorHandler'))).toBe(true);
  });

  it('getImpactRadius(AppError) includes utils/errors and middleware/errorHandler', () => {
    const { files } = traversal.getImpactRadius('AppError');
    const normalized = files.map((f) => f.replace(/\\/g, '/'));
    expect(normalized.some((f) => f.includes('utils/errors'))).toBe(true);
    expect(normalized.some((f) => f.includes('errorHandler'))).toBe(true);
  });

  it('findCallees for middleware/auth returns auth/verify', () => {
    const callees = traversal.findCallees('authMiddleware', 'src/middleware/auth.ts');
    const fromVerify = callees.filter((r) => r.referenced_in_file.replace(/\\/g, '/').includes('auth/verify'));
    expect(fromVerify.length).toBeGreaterThanOrEqual(1);
  });

  it('findConnectedFiles respects maxDepth', () => {
    const entry = 'src/users/queries.ts';
    const depth1 = traversal.findConnectedFiles([entry], 1);
    const depth2 = traversal.findConnectedFiles([entry], 2);
    const maxDist1 = Math.max(...depth1.map((n) => n.distance));
    const maxDist2 = Math.max(...depth2.map((n) => n.distance));
    expect(maxDist1).toBeLessThanOrEqual(1);
    expect(maxDist2).toBeLessThanOrEqual(2);
  });
});
