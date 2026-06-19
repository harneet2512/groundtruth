/**
 * CLI setup command tests.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { join, resolve } from 'path';
import { existsSync, readFileSync, rmSync, mkdirSync } from 'fs';
import { tmpdir } from 'os';

describe('handleSetup', () => {
  let tmpDir: string;
  const originalCwd = process.cwd();
  const logs: string[] = [];
  const errorLogs: string[] = [];

  beforeEach(() => {
    tmpDir = join(tmpdir(), `gt-test-setup-${Date.now()}`);
    mkdirSync(tmpDir, { recursive: true });
    process.chdir(tmpDir);
    process.exitCode = undefined;
    logs.length = 0;
    errorLogs.length = 0;
    vi.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      logs.push(args.join(' '));
    });
    vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      errorLogs.push(args.join(' '));
    });
  });

  afterEach(() => {
    process.chdir(originalCwd);
    process.exitCode = undefined;
    vi.restoreAllMocks();
    if (existsSync(tmpDir)) {
      rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  it('writes .cursor/mcp.json with --cursor', async () => {
    const { handleSetup } = await import('../../../src/cli/commands/setup.js');
    await handleSetup({ cursor: true });

    const configPath = join(tmpDir, '.cursor', 'mcp.json');
    expect(existsSync(configPath)).toBe(true);

    const config = JSON.parse(readFileSync(configPath, 'utf-8'));
    expect(config.mcpServers.groundtruth.command).toBe('node');
    // Check that the args contain the server path (use resolve for cross-platform)
    const expectedPath = resolve(tmpDir, 'dist/mcp/server.js');
    expect(config.mcpServers.groundtruth.args[0]).toBe(expectedPath);
  });

  it('writes .claude/mcp.json with --claude-code', async () => {
    const { handleSetup } = await import('../../../src/cli/commands/setup.js');
    await handleSetup({ claudeCode: true });

    const configPath = join(tmpDir, '.claude', 'mcp.json');
    expect(existsSync(configPath)).toBe(true);

    const config = JSON.parse(readFileSync(configPath, 'utf-8'));
    expect(config.mcpServers.groundtruth.command).toBe('node');
  });

  it('exits with code 1 if neither flag specified', async () => {
    const { handleSetup } = await import('../../../src/cli/commands/setup.js');
    await handleSetup({});

    expect(process.exitCode).toBe(1);
    expect(errorLogs.join('\n')).toContain('--cursor');
  });

  it('writes both configs when --cursor and --claude-code are set', async () => {
    const { handleSetup } = await import('../../../src/cli/commands/setup.js');
    await handleSetup({ cursor: true, claudeCode: true });

    const cursorPath = join(tmpDir, '.cursor', 'mcp.json');
    const claudePath = join(tmpDir, '.claude', 'mcp.json');
    expect(existsSync(cursorPath)).toBe(true);
    expect(existsSync(claudePath)).toBe(true);

    const cursorConfig = JSON.parse(readFileSync(cursorPath, 'utf-8'));
    const claudeConfig = JSON.parse(readFileSync(claudePath, 'utf-8'));
    expect(cursorConfig.mcpServers.groundtruth.command).toBe('node');
    expect(claudeConfig.mcpServers.groundtruth.command).toBe('node');
  });
});
