/**
 * CLI helpers tests — openStore.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { join } from 'path';
import { existsSync, rmSync, mkdirSync } from 'fs';
import { tmpdir } from 'os';
import { defaultConfig } from '../../../src/config/defaults.js';

describe('openStore', () => {
  let tmpDir: string;
  const originalCwd = process.cwd();

  beforeEach(() => {
    tmpDir = join(tmpdir(), `gt-test-helpers-${Date.now()}`);
    mkdirSync(tmpDir, { recursive: true });
    process.chdir(tmpDir);
  });

  afterEach(() => {
    process.chdir(originalCwd);
    if (existsSync(tmpDir)) {
      rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  it('creates store and DB when no mustExist constraint', async () => {
    const { openStore } = await import('../../../src/cli/helpers.js');
    const ctx = openStore();
    try {
      expect(ctx.store).toBeDefined();
      expect(ctx.projectRoot).toBe(process.cwd());
      expect(existsSync(ctx.dbPath)).toBe(true);
    } finally {
      ctx.store.close();
    }
  });

  it('throws when mustExist is true and DB does not exist', async () => {
    const { openStore } = await import('../../../src/cli/helpers.js');
    expect(() => openStore({ mustExist: true })).toThrow('No GroundTruth index found');
  });
});
