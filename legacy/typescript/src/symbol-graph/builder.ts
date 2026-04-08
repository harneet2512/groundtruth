/**
 * Symbol Graph Builder — Crawls the project and populates SQLite.
 *
 * On startup, walks every TypeScript file in the project and asks
 * the LSP for its exports. Stores each symbol with its signature
 * and file location. Also records import relationships between files.
 *
 * This is the most expensive operation (~10-30s for a large project)
 * but only runs once at startup. After that, the file watcher
 * handles incremental updates.
 */

import Database from "better-sqlite3";
import { LSPManager } from "../lsp/manager.js";
import { glob } from "fs/promises";
import { logger } from "../utils/logger.js";

export async function buildGraph(
  db: Database.Database,
  projectRoot: string,
  lsp: LSPManager
): Promise<number> {
  // Clear existing data for fresh build
  db.exec("DELETE FROM symbols");
  db.exec("DELETE FROM imports");

  // Find all TypeScript files
  const files: string[] = [];
  // TODO: Use glob to find all .ts/.tsx files, excluding node_modules

  const insertSymbol = db.prepare(`
    INSERT INTO symbols (name, kind, signature, file_path, line)
    VALUES (?, ?, ?, ?, ?)
  `);

  const insertImport = db.prepare(`
    INSERT INTO imports (from_file, to_file, symbol_name)
    VALUES (?, ?, ?)
  `);

  let symbolCount = 0;

  const buildAll = db.transaction(() => {
    for (const file of files) {
      // TODO: Get exports from LSP
      // TODO: Parse imports from AST
      // TODO: Insert into SQLite
    }
  });

  buildAll();

  logger.info(`Indexed ${files.length} files, ${symbolCount} symbols`);
  return symbolCount;
}
