/**
 * CLI: groundtruth setup --cursor | --claude-code
 * Writes MCP config for the chosen AI coding tool.
 */
import { existsSync, mkdirSync, writeFileSync } from 'fs';
import { join, resolve } from 'path';

export async function handleSetup(options: {
  cursor?: boolean;
  claudeCode?: boolean;
}): Promise<void> {
  if (!options.cursor && !options.claudeCode) {
    console.error('Error: Specify --cursor or --claude-code');
    process.exitCode = 1;
    return;
  }

  const projectRoot = process.cwd();
  const serverPath = resolve(projectRoot, 'dist/mcp/server.js');

  const mcpConfig = {
    mcpServers: {
      groundtruth: {
        command: 'node',
        args: [serverPath],
      },
    },
  };

  const targets: Array<{ name: string; dir: string }> = [];

  if (options.cursor) {
    targets.push({ name: 'Cursor', dir: '.cursor' });
  }
  if (options.claudeCode) {
    targets.push({ name: 'Claude Code', dir: '.claude' });
  }

  try {
    for (const target of targets) {
      const configDir = join(projectRoot, target.dir);
      if (!existsSync(configDir)) {
        mkdirSync(configDir, { recursive: true });
      }
      const configPath = join(configDir, 'mcp.json');
      writeFileSync(configPath, JSON.stringify(mcpConfig, null, 2) + '\n', 'utf-8');
      console.log(`Wrote ${target.name} config: ${configPath}`);
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`Error: ${msg}`);
    process.exitCode = 1;
  }
}
