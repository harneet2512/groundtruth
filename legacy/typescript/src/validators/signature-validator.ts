/**
 * Signature validator — parse function calls, check arg counts against indexed signatures.
 * Uses bracket depth tracking to avoid false positives on nested calls.
 */
import type { SymbolStore } from '../symbol-graph/sqlite-store.js';

export interface SignatureError {
  type: 'signature_mismatch';
  symbol: string;
  location: string;
  message: string;
  expectedSignature?: string;
}

interface ParamInfo {
  name: string;
  type: string;
  optional?: boolean;
}

function countArgs(argsStr: string): number | null {
  const trimmed = argsStr.trim();
  if (trimmed === '') return 0;

  let depth = 0;
  let count = 1;
  let inString: string | null = null;

  for (let i = 0; i < trimmed.length; i++) {
    const ch = trimmed[i];

    // Track string literals
    if (inString) {
      if (ch === inString && trimmed[i - 1] !== '\\') {
        inString = null;
      }
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      inString = ch;
      continue;
    }

    if (ch === '(' || ch === '[' || ch === '{') {
      depth++;
    } else if (ch === ')' || ch === ']' || ch === '}') {
      depth--;
      if (depth < 0) return null; // malformed
    } else if (ch === ',' && depth === 0) {
      count++;
    }
  }

  // If still in a string or unbalanced brackets, skip
  if (depth !== 0 || inString !== null) return null;

  return count;
}

function parseCalls(code: string): Array<{ name: string; argsStr: string }> {
  const results: Array<{ name: string; argsStr: string }> = [];
  // Match function calls: identifier followed by (
  const callRe = /\b([a-zA-Z_$]\w*)\s*\(/g;
  let match: RegExpExecArray | null;

  // Keywords that look like function calls but aren't
  const keywords = new Set([
    'if', 'for', 'while', 'switch', 'catch', 'function', 'class',
    'return', 'typeof', 'instanceof', 'new', 'throw', 'import',
    'export', 'const', 'let', 'var', 'async', 'await', 'yield',
  ]);

  while ((match = callRe.exec(code)) !== null) {
    const name = match[1];
    if (keywords.has(name)) continue;

    // Extract the arguments by tracking parens
    const startIdx = match.index + match[0].length;
    let depth = 1;
    let i = startIdx;
    let inString: string | null = null;

    while (i < code.length && depth > 0) {
      const ch = code[i];
      if (inString) {
        if (ch === inString && code[i - 1] !== '\\') {
          inString = null;
        }
      } else if (ch === '"' || ch === "'" || ch === '`') {
        inString = ch;
      } else if (ch === '(') {
        depth++;
      } else if (ch === ')') {
        depth--;
      }
      i++;
    }

    if (depth === 0) {
      const argsStr = code.slice(startIdx, i - 1);
      results.push({ name, argsStr });
    }
  }

  return results;
}

export function validateSignatures(
  store: SymbolStore,
  code: string,
  filePath: string
): SignatureError[] {
  const errors: SignatureError[] = [];
  const calls = parseCalls(code);
  const checked = new Set<string>();

  for (const { name, argsStr } of calls) {
    // Only check each function name once (first occurrence)
    if (checked.has(name)) continue;
    checked.add(name);

    const symbols = store.getExportedSymbolsByName(name);
    if (symbols.length === 0) continue;

    const symbol = symbols[0];
    if (!symbol.params) continue;

    let params: ParamInfo[];
    try {
      params = JSON.parse(symbol.params) as ParamInfo[];
    } catch {
      continue;
    }

    if (!Array.isArray(params) || params.length === 0) continue;

    const actualCount = countArgs(argsStr);
    if (actualCount === null) continue; // ambiguous, skip

    const requiredCount = params.filter((p) => !p.optional && !p.type?.includes('undefined')).length;
    const totalCount = params.length;

    if (actualCount < requiredCount || actualCount > totalCount) {
      errors.push({
        type: 'signature_mismatch',
        symbol: name,
        location: filePath,
        message: `'${name}' expects ${requiredCount === totalCount ? requiredCount : `${requiredCount}-${totalCount}`} arguments, but got ${actualCount}.`,
        expectedSignature: symbol.signature ?? undefined,
      });
    }
  }

  return errors;
}
