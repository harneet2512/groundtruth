/**
 * Common LSP Queries — Shared query patterns used by the validator.
 *
 * Wraps LSPManager methods into higher-level queries:
 * - "Does this import resolve?" (path + symbol check)
 * - "Does this function call match the actual signature?"
 * - "Does this type exist and have these properties?"
 *
 * Keeps the validator clean — it calls queries, queries call the LSP.
 */

import { LSPManager, ResolvedSymbol } from "./manager.js";

/**
 * Check if an import statement resolves to a real export.
 * Returns the resolved symbol if valid, null if hallucinated.
 */
export async function checkImport(
  importPath: string,
  symbolName: string,
  fromFile: string,
  lsp: LSPManager
): Promise<ResolvedSymbol | null> {
  // TODO: Resolve importPath relative to fromFile
  // TODO: Check if symbolName is exported from resolved path
  return lsp.resolveSymbol(symbolName, importPath);
}

/**
 * Check if a function call matches the actual signature.
 * Returns null if valid, mismatch details if not.
 */
export async function checkSignature(
  functionName: string,
  argCount: number,
  filePath: string,
  lsp: LSPManager
): Promise<SignatureMismatch | null> {
  const resolved = await lsp.resolveSymbol(functionName, filePath);
  if (!resolved) return { reason: "function_not_found" };

  // TODO: Parse signature to check arg count, types
  return null;
}

export interface SignatureMismatch {
  reason: string;
  expected?: string;
  actual?: string;
}
