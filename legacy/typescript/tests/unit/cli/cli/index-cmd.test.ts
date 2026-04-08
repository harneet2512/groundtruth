/**
 * CLI index command tests.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { join } from 'path';
import { existsSync, mkdirSync, rmSync } from 'fs';
import { tmpdir } from 'os';

const FIXTURE = join(process.cwd(), 'tests', 'fixtures', 'test-project');

// Use a unique DB dir per test file to avoid parallel conflicts
const DB_DIR_NAME = `.groundtruth-test-index-${process.pid}`;
vi.mock('../../../src/config/defaults.js', () => ({
  defaultConfig: {
    dbPath: `${DB_DIR_NAME}/graph.sqlite`,
    maxLevenshteinDistance: 3,
    ftsLimit: 20,
  },
}));

describe('handleIndex', () => {
  const originalCwd = process.cwd();
  const logs: string[] = [];

  beforeEach(() => {
    logs.length = 0;
    vi.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      logs.push(args.join(' '));
    });
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

  it('indexes fixture project and prints stats', async () => {
    const { handleIndex } = await import('../../../src/cli/commands/index-cmd.js');
    await handleIndex();

    const output = logs.join('\n');
    expect(output).toContain('Indexing project...');
    expect(output).toContain('Symbols:');
    expect(output).toContain('Done.');

    // DB file should exist
    const dbPath = join(FIXTURE, DB_DIR_NAME, 'graph.sqlite');
    expect(existsSync(dbPath)).toBe(true);
  });
});
