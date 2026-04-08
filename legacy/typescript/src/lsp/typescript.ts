/**
 * TypeScript LSP Client — Communicates with tsserver.
 *
 * This is the most critical file in the project. tsserver gives us
 * deterministic answers about what exists in a TypeScript codebase:
 * - Every export from every file
 * - Full type signatures
 * - Import resolution (does this path resolve?)
 * - Definition locations
 *
 * We use ts-morph as the primary interface because it wraps the
 * TypeScript compiler API directly — no separate process, no IPC
 * overhead. For operations ts-morph can't handle, we fall back to
 * spawning tsserver via vscode-languageserver-node.
 */

import { ExportedSymbol, ResolvedSymbol, SymbolLocation } from "./manager.js";

export class TypeScriptClient {
  private projectRoot: string;
  private connected = false;
  // TODO: ts-morph Project instance

  constructor(projectRoot: string) {
    this.projectRoot = projectRoot;
  }

  async start(): Promise<void> {
    // TODO: Initialize ts-morph Project with tsconfig.json
    // TODO: Load all source files into the project
    this.connected = true;
  }

  async stop(): Promise<void> {
    this.connected = false;
  }

  isConnected(): boolean {
    return this.connected;
  }

  async resolveSymbol(
    symbolName: string,
    filePath: string
  ): Promise<ResolvedSymbol | null> {
    // TODO: Use ts-morph to check if symbolName is exported from filePath
    // TODO: Return full signature if found
    return null;
  }

  async getExports(filePath: string): Promise<ExportedSymbol[]> {
    // TODO: Use ts-morph to get all exports from the file
    // This powers the symbol graph builder
    return [];
  }

  async findDefinition(symbolName: string): Promise<SymbolLocation[]> {
    // TODO: Search project for where symbolName is defined
    return [];
  }
}
