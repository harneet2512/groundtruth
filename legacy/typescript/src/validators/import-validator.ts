/**
 * Import validator — parse imports from code, check against symbol store.
 * Uses resolveImport() from query.ts for relative imports.
 */
import type { SymbolStore } from '../symbol-graph/sqlite-store.js';
import { resolveImport } from '../symbol-graph/query.js';

export interface ImportError {
  type: 'symbol_not_found' | 'module_not_found';
  symbol: string;
  location: string;
  message: string;
  suggestions?: string[];
  available?: string[];
  bestDistance?: number;
}

interface ParsedImport {
  names: Array<{ name: string; isDefault: boolean }>;
  source: string;
}

function parseImports(code: string): ParsedImport[] {
  const results: ParsedImport[] = [];

  // Named imports: import { a, b } from './foo'
  const namedRe = /import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"]/g;
  let match: RegExpExecArray | null;
  while ((match = namedRe.exec(code)) !== null) {
    const names = match[1].split(',').map((n) => {
      const parts = n.trim().split(/\s+as\s+/);
      return { name: parts[0].trim(), isDefault: false };
    }).filter((n) => n.name.length > 0);
    results.push({ names, source: match[2] });
  }

  // Default imports: import Foo from './foo'
  const defaultRe = /import\s+(\w+)\s+from\s+['"]([^'"]+)['"]/g;
  while ((match = defaultRe.exec(code)) !== null) {
    // Skip if this is actually "import type X from"
    if (match[1] === 'type') continue;
    results.push({ names: [{ name: match[1], isDefault: true }], source: match[2] });
  }

  return results;
}

export function validateImports(
  store: SymbolStore,
  code: string,
  filePath: string
): ImportError[] {
  const errors: ImportError[] = [];
  const imports = parseImports(code);

  for (const imp of imports) {
    // Skip non-relative imports (handled by package-validator)
    if (!imp.source.startsWith('.')) continue;

    for (const { name, isDefault } of imp.names) {
      const importName = isDefault ? 'default' : name;
      const result = resolveImport(store, imp.source, importName, filePath);

      if (!result.found) {
        // Check if the module has any exports at all
        const hasExports = result.available && result.available.length > 0;
        if (hasExports) {
          errors.push({
            type: 'symbol_not_found',
            symbol: name,
            location: `${filePath} → ${imp.source}`,
            message: `Module '${imp.source}' does not export '${name}'.`,
            suggestions: result.suggestions,
            available: result.available,
            bestDistance: result.bestDistance,
          });
        } else {
          errors.push({
            type: 'module_not_found',
            symbol: name,
            location: `${filePath} → ${imp.source}`,
            message: `Module '${imp.source}' not found in symbol index.`,
          });
        }
      }
    }
  }

  return errors;
}
