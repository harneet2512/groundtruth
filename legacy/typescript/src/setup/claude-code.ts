/**
 * Claude Code Setup — Configures MCP server + generates CLAUDE.md.
 *
 * Two things happen:
 * 1. Adds GroundTruth as an MCP server in Claude Code's settings
 * 2. Creates/updates .claude/CLAUDE.md with instructions telling
 *    Claude to use groundtruth_generate before writing code
 */

import { readFile, writeFile, mkdir } from "fs/promises";
import { join } from "path";
import { existsSync } from "fs";

export async function setupClaudeCode(projectRoot: string): Promise<void> {
  // Generate .claude/CLAUDE.md with GroundTruth instructions
  const claudeDir = join(projectRoot, ".claude");
  if (!existsSync(claudeDir)) {
    await mkdir(claudeDir, { recursive: true });
  }

  const templatePath = join(import.meta.dirname, "templates", "CLAUDE.md");
  const template = await readFile(templatePath, "utf-8");

  const claudeMdPath = join(claudeDir, "CLAUDE.md");
  if (existsSync(claudeMdPath)) {
    // Append to existing CLAUDE.md
    const existing = await readFile(claudeMdPath, "utf-8");
    if (!existing.includes("groundtruth")) {
      await writeFile(claudeMdPath, existing + "\n\n" + template);
    }
  } else {
    await writeFile(claudeMdPath, template);
  }

  // TODO: Add MCP server config to Claude Code settings
  // Location: ~/.claude/settings.json or project .claude/settings.json
}
