/**
 * CLI status command tests.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { join } from 'path';
import { existsSync, rmSync, mkdirSync } from 'fs';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';
import { ProjectIndexer } from '../../../src/symbol-graph/indexer.js';

const FIXTURE = join(process.cwd(), 'tests', 'fixtures', 'test-project');
const DB_DIR_NAME = `.groundtruth-test-status-${process.pid}`;

vi.mock('../../../src/config/defaults.js', () => ({
  defaultConfig: {
    dbPath: `${DB_DIR_NAME}/graph.sqlite`,
    maxLevenshteinDistance: 3,
    ftsLimit: 20,
  },
}));

describe('handleStatus', () => {
  const originalCwd = process.cwd();
  const logs: string[] = [];

  beforeEach(async () => {
    logs.length = 0;
    vi.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      logs.push(args.join(' '));
    });

    const dbDir = join(FIXTURE, DB_DIR_NAME);
    if (!existsSync(dbDir)) {
      mkdirSync(dbDir, { recursive: true });
    }
    const store = new SymbolStore(join(dbDir, 'graph.sqlite'));
    store.init();
    const indexer = new ProjectIndexer(store);
    await indexer.indexProject(FIXTURE);
    store.close();

    process.chdir(FIXTURE);
  });

  afterEach(() => {
    process.chdir(originalCwd);
    vi.restoreAllMocks();
    try {
      rmSync(join(FIXTURE, DB_DIR_NAME), { recursive: true, force: true });
    } catch {
      // ignore EBUSY on Windows
    }
  });

  it('shows symbol count in status output', async () => {
    const { handleStatus } = await import('../../../src/cli/commands/status.js');
    await handleStatus();

    const output = logs.join('\n');
    expect(output).toContain('GroundTruth Status');
    expect(output).toContain('Symbols:');
    expect(output).not.toContain('Symbols:        0');
  });
});
