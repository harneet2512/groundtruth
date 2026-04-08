/**
 * File Watcher — Keeps the symbol graph current on file changes.
 *
 * Uses chokidar to watch for .ts/.tsx file changes. When a file changes:
 * 1. Delete its symbols from the graph
 * 2. Re-query LSP for its current exports
 * 3. Insert updated symbols
 *
 * This is incremental — only the changed file is re-indexed, not the
 * entire project. Should be <100ms per file change.
 */

import Database from "better-sqlite3";
import { LSPManager } from "../lsp/manager.js";
import { logger } from "../utils/logger.js";

export function startWatcher(
  db: Database.Database,
  projectRoot: string,
  lsp: LSPManager
): void {
  // TODO: Initialize chokidar watcher
  // TODO: Watch for .ts/.tsx file changes (exclude node_modules)
  // TODO: On change: re-index the changed file
  // TODO: On delete: remove file's symbols from graph
  // TODO: On add: index the new file

  logger.info("File watcher started");
}

async function reindexFile(
  db: Database.Database,
  filePath: string,
  lsp: LSPManager
): Promise<void> {
  // Delete existing symbols for this file
  db.prepare("DELETE FROM symbols WHERE file_path = ?").run(filePath);
  db.prepare(
    "DELETE FROM imports WHERE from_file = ? OR to_file = ?"
  ).run(filePath, filePath);

  // TODO: Re-query LSP for exports
  // TODO: Re-parse AST for imports
  // TODO: Insert updated data
}
