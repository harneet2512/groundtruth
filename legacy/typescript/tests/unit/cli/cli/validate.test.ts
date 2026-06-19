/**
 * CLI validate command tests.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { join } from 'path';
import { existsSync, rmSync, writeFileSync, mkdirSync } from 'fs';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';
import { ProjectIndexer } from '../../../src/symbol-graph/indexer.js';

const FIXTURE = join(process.cwd(), 'tests', 'fixtures', 'test-project');
const DB_DIR_NAME = `.groundtruth-test-validate-${process.pid}`;

vi.mock('../../../src/config/defaults.js', () => ({
  defaultConfig: {
    dbPath: `${DB_DIR_NAME}/graph.sqlite`,
    maxLevenshteinDistance: 3,
    ftsLimit: 20,
  },
}));

describe('handleValidate', () => {
  const originalCwd = process.cwd();
  const logs: string[] = [];
  const errorLogs: string[] = [];

  beforeEach(async () => {
    logs.length = 0;
    errorLogs.length = 0;
    process.exitCode = undefined;
    vi.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      logs.push(args.join(' '));
    });
    vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      errorLogs.push(args.join(' '));
    });

    // Pre-index the fixture project
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
    process.exitCode = undefined;
    vi.restoreAllMocks();
    try {
      rmSync(join(FIXTURE, DB_DIR_NAME), { recursive: true, force: true });
    } catch {
      // ignore EBUSY on Windows
    }
  });

  it('prints VALID for a clean file', async () => {
    const { handleValidate } = await import('../../../src/cli/commands/validate.js');
    await handleValidate(join(FIXTURE, 'src', 'index.ts'));

    const output = logs.join('\n');
    expect(output).toContain('VALID');
    expect(process.exitCode).toBeUndefined();
  });

  it('prints error for nonexistent file', async () => {
    const { handleValidate } = await import('../../../src/cli/commands/validate.js');
    await handleValidate('nonexistent.ts');

    const output = errorLogs.join('\n');
    expect(output).toContain('File not found');
    expect(process.exitCode).toBe(1);
  });

  it('reports errors for file with bad import', async () => {
    const badFile = join(FIXTURE, 'src', '_test_bad.ts');
    writeFileSync(badFile, `import { nonExistentThing } from './utils/crypto.js';\n`, 'utf-8');
    try {
      const { handleValidate } = await import('../../../src/cli/commands/validate.js');
      await handleValidate(badFile);

      const output = logs.join('\n');
      expect(output).toContain('error');
      expect(process.exitCode).toBe(1);
    } finally {
      rmSync(badFile, { force: true });
    }
  });
});
