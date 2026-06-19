/**
 * Validator Orchestrator — Parses code, runs all validation checks.
 *
 * Takes proposed code as a string (NOT a file on disk — this is key).
 * The code hasn't been written yet. We're validating it in memory.
 *
 * Steps:
 * 1. Parse AST to extract all imports, function calls, type references
 * 2. For each extracted symbol, check against LSP
 * 3. Collect all errors with their types (import, signature, type, path)
 * 4. Return structured validation result
 */

import { LSPManager } from "../lsp/manager.js";
import { validateImports } from "./imports.js";
import { validateFunctions } from "./functions.js";
import { validateTypes } from "./types.js";
import { parseAST } from "../utils/ast.js";

export interface ValidationError {
  symbol: string;
  type: "import" | "signature" | "type" | "path";
  reason: string;
  line?: number;
}

export interface ValidationResult {
  errors: ValidationError[];
}

export async function validateCode(
  code: string,
  lsp: LSPManager
): Promise<ValidationResult> {
  // Parse AST to extract symbols
  const ast = parseAST(code);

  // Run all validators in parallel
  const [importErrors, functionErrors, typeErrors] = await Promise.all([
    validateImports(ast.imports, lsp),
    validateFunctions(ast.functionCalls, lsp),
    validateTypes(ast.typeReferences, lsp),
  ]);

  return {
    errors: [...importErrors, ...functionErrors, ...typeErrors],
  };
}
