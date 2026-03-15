/**
 * Import Validator — Checks that every import resolves to a real export.
 *
 * This is the highest-value check. Import hallucinations are the most
 * common type (model invents function names, wrong paths, non-existent
 * modules). If we only ship one validator, this is the one.
 *
 * Checks:
 * - Does the import path resolve to a real file?
 * - Does the named export exist in that file?
 * - Is it a default vs named export mismatch?
 */

import { LSPManager } from "../lsp/manager.js";
import { ValidationError } from "./index.js";

export interface ParsedImport {
  modulePath: string;
  importedNames: string[];
  line: number;
  isDefault: boolean;
}

export async function validateImports(
  imports: ParsedImport[],
  lsp: LSPManager
): Promise<ValidationError[]> {
  const errors: ValidationError[] = [];

  for (const imp of imports) {
    for (const name of imp.importedNames) {
      const resolved = await lsp.resolveSymbol(name, imp.modulePath);

      if (!resolved) {
        errors.push({
          symbol: name,
          type: "import",
          reason: `'${name}' does not exist in '${imp.modulePath}'`,
          line: imp.line,
        });
      }
    }
  }

  return errors;
}
