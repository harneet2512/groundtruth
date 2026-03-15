/**
 * Cursor Setup — Configures MCP server + generates .cursorrules.
 *
 * Two things happen:
 * 1. Adds GroundTruth as an MCP server in Cursor's MCP config
 * 2. Creates/updates .cursorrules with instructions telling
 *    Cursor to use groundtruth_generate before writing code
 */

import { readFile, writeFile } from "fs/promises";
import { join } from "path";
import { existsSync } from "fs";

export async function setupCursor(projectRoot: string): Promise<void> {
  const templatePath = join(import.meta.dirname, "templates", "cursorrules");
  const template = await readFile(templatePath, "utf-8");

  const rulesPath = join(projectRoot, ".cursorrules");
  if (existsSync(rulesPath)) {
    const existing = await readFile(rulesPath, "utf-8");
    if (!existing.includes("groundtruth")) {
      await writeFile(rulesPath, existing + "\n\n" + template);
    }
  } else {
    await writeFile(rulesPath, template);
  }

  // TODO: Add MCP server config to Cursor's settings
}
