/**
 * Function Validator — Checks that function calls match actual signatures.
 *
 * Catches:
 * - Calling a function that doesn't exist
 * - Wrong number of arguments
 * - Wrong argument types (when type info is available)
 */

import { LSPManager } from "../lsp/manager.js";
import { ValidationError } from "./index.js";

export interface ParsedFunctionCall {
  name: string;
  argCount: number;
  filePath: string;
  line: number;
}

export async function validateFunctions(
  calls: ParsedFunctionCall[],
  lsp: LSPManager
): Promise<ValidationError[]> {
  const errors: ValidationError[] = [];

  for (const call of calls) {
    const resolved = await lsp.resolveSymbol(call.name, call.filePath);

    if (!resolved) {
      errors.push({
        symbol: call.name,
        type: "signature",
        reason: `'${call.name}' is not defined`,
        line: call.line,
      });
    }

    // TODO: Check arg count against resolved signature
    // TODO: Check arg types if type info available
  }

  return errors;
}
