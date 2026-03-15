/**
 * LSP Manager — Starts, stops, and queries language servers.
 *
 * MVP: TypeScript only (tsserver via vscode-languageserver-node).
 * V0.2: Add Pyright for Python.
 * V0.3: Add gopls for Go.
 *
 * The manager handles:
 * - Starting the language server for the project
 * - Querying symbols (does this export exist? what's its signature?)
 * - Graceful shutdown
 *
 * We talk to tsserver, not the LSP protocol directly — tsserver is
 * TypeScript's own language service and gives us the richest data.
 */

import { TypeScriptClient } from "./typescript.js";

export class LSPManager {
  private tsClient: TypeScriptClient;
  private projectRoot: string;

  constructor(projectRoot: string) {
    this.projectRoot = projectRoot;
    this.tsClient = new TypeScriptClient(projectRoot);
  }

  async start(): Promise<void> {
    await this.tsClient.start();
  }

  async stop(): Promise<void> {
    await this.tsClient.stop();
  }

  /**
   * Check if a symbol exists in the given file.
   * Returns the symbol's full signature if found, null if not.
   */
  async resolveSymbol(
    symbolName: string,
    filePath: string
  ): Promise<ResolvedSymbol | null> {
    return this.tsClient.resolveSymbol(symbolName, filePath);
  }

  /**
   * Get all exports from a file.
   */
  async getExports(filePath: string): Promise<ExportedSymbol[]> {
    return this.tsClient.getExports(filePath);
  }

  /**
   * Find where a symbol is defined across the project.
   */
  async findDefinition(symbolName: string): Promise<SymbolLocation[]> {
    return this.tsClient.findDefinition(symbolName);
  }

  getStatus(): string {
    return this.tsClient.isConnected()
      ? "tsserver (connected)"
      : "tsserver (disconnected)";
  }
}

// Types co-located here because they're small and used by both
// the LSP manager and its consumers.

export interface ResolvedSymbol {
  name: string;
  kind: "function" | "class" | "type" | "interface" | "variable" | "enum";
  signature: string;
  filePath: string;
}

export interface ExportedSymbol {
  name: string;
  kind: string;
  signature: string;
}

export interface SymbolLocation {
  filePath: string;
  line: number;
  column: number;
}
