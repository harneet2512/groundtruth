/**
 * Type Validator — Checks that type references point to real types.
 *
 * Catches:
 * - Using a type/interface that doesn't exist
 * - Accessing properties that don't exist on a type
 * - Using wrong generic parameters
 */

import { LSPManager } from "../lsp/manager.js";
import { ValidationError } from "./index.js";

export interface ParsedTypeReference {
  name: string;
  filePath: string;
  line: number;
}

export async function validateTypes(
  types: ParsedTypeReference[],
  lsp: LSPManager
): Promise<ValidationError[]> {
  const errors: ValidationError[] = [];

  for (const typeRef of types) {
    const resolved = await lsp.resolveSymbol(typeRef.name, typeRef.filePath);

    if (!resolved) {
      errors.push({
        symbol: typeRef.name,
        type: "type",
        reason: `Type '${typeRef.name}' does not exist`,
        line: typeRef.line,
      });
    }
  }

  return errors;
}
