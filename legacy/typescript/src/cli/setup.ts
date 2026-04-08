/**
 * CLI: groundtruth setup --claude-code | --cursor
 *
 * Auto-configures MCP integration for the specified tool:
 * 1. Writes MCP server config to the tool's settings file
 * 2. Generates project-level instructions (.claude/CLAUDE.md or .cursorrules)
 *    that tell the model to use GroundTruth before writing code
 */

import { setupClaudeCode } from "../setup/claude-code.js";
import { setupCursor } from "../setup/cursor.js";

export async function handleSetup(options: {
  claudeCode?: boolean;
  cursor?: boolean;
}): Promise<void> {
  if (options.claudeCode) {
    await setupClaudeCode(process.cwd());
    console.log("✓ Claude Code configured");
  }

  if (options.cursor) {
    await setupCursor(process.cwd());
    console.log("✓ Cursor configured");
  }

  if (!options.claudeCode && !options.cursor) {
    console.log("Specify a tool: --claude-code or --cursor");
    process.exit(1);
  }
}
