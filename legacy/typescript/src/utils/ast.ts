/**
 * AST Parser — Extracts imports, function calls, and type references from code.
 *
 * Uses ts-morph to parse TypeScript code strings (not files on disk).
 * This is critical for groundtruth_generate — we're parsing proposed
 * code that doesn't exist as a file yet.
 *
 * Returns structured data that the validator checks against the LSP.
 */

import { ParsedImport } from "../validator/imports.js";
import { ParsedFunctionCall } from "../validator/functions.js";
import { ParsedTypeReference } from "../validator/types.js";

export interface ParsedAST {
  imports: ParsedImport[];
  functionCalls: ParsedFunctionCall[];
  typeReferences: ParsedTypeReference[];
}

export function parseAST(code: string): ParsedAST {
  // TODO: Create ts-morph SourceFile from code string
  // TODO: Walk AST to extract:
  //   - ImportDeclarations → ParsedImport[]
  //   - CallExpressions → ParsedFunctionCall[]
  //   - TypeReferences → ParsedTypeReference[]

  return {
    imports: [],
    functionCalls: [],
    typeReferences: [],
  };
}
