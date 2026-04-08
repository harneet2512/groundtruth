/**
 * CLI stats command tests.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { join } from 'path';
import { existsSync, rmSync, mkdirSync } from 'fs';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';
import { defaultConfig } from '../../../src/config/defaults.js';

const FIXTURE = join(process.cwd(), 'tests', 'fixtures', 'test-project');

describe('handleStats', () => {
  const originalCwd = process.cwd();
  const logs: string[] = [];
  let dbDir: string;

  beforeEach(() => {
    logs.length = 0;
    vi.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      logs.push(args.join(' '));
    });

    dbDir = join(FIXTURE, '.groundtruth');
    if (!existsSync(dbDir)) {
      mkdirSync(dbDir, { recursive: true });
    }
    const dbPath = join(FIXTURE, defaultConfig.dbPath);
    const store = new SymbolStore(dbPath);
    store.init();
    store.close();

    process.chdir(FIXTURE);
  });

  afterEach(() => {
    process.chdir(originalCwd);
    vi.restoreAllMocks();
    try {
      rmSync(dbDir, { recursive: true, force: true });
    } catch {
      // ignore EBUSY on Windows
    }
  });

  it('outputs text format by default', async () => {
    const { handleStats } = await import('../../../src/cli/commands/stats.js');
    await handleStats({ days: '7' });

    const output = logs.join('\n');
    expect(output).toContain('GroundTruth');
    expect(output).toContain('Total validations:');
  });

  it('outputs valid JSON with --json flag', async () => {
    const { handleStats } = await import('../../../src/cli/commands/stats.js');
    await handleStats({ json: true, days: '7' });

    const output = logs.join('\n');
    const parsed = JSON.parse(output);
    expect(parsed).toHaveProperty('total');
    expect(parsed).toHaveProperty('clean');
    expect(parsed).toHaveProperty('caught');
    expect(parsed).toHaveProperty('fixed');
  });

  it('rejects invalid --days value', async () => {
    const errorLogs: string[] = [];
    vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      errorLogs.push(args.join(' '));
    });
    process.exitCode = undefined;

    const { handleStats } = await import('../../../src/cli/commands/stats.js');
    await handleStats({ days: 'abc' });

    expect(process.exitCode).toBe(1);
    expect(errorLogs.join('\n')).toContain('--days must be a positive integer');
  });
});
