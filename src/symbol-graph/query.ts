/**
 * Query layer — resolveImport, searchSymbolByName (cross-index), searchSymbolsSemantic (FTS5).
 * Used by validators and briefing engine.
 */
import { dirname, join, normalize } from 'path';
import type { SymbolStore } from './sqlite-store.js';
import { suggestAlternativesWithDistance } from '../utils/levenshtein.js';

export interface ResolveImportResult {
  found: boolean;
  available?: string[];
  suggestions?: string[];
  bestDistance?: number;
}

/**
 * Resolve modulePath relative to fromFile to a key used in exports, then check if importName exists.
 */
export function resolveImport(
  store: SymbolStore,
  modulePath: string,
  importName: string,
  fromFile: string
): ResolveImportResult {
  const resolved = normalize(join(dirname(fromFile), modulePath)).replace(/\\/g, '/');
  const withoutExt = resolved.replace(/\.(ts|tsx|js|jsx)$/i, '');
  const candidates = [withoutExt, `${withoutExt}/index`];
  let exports: Array<{ name: string }> = [];
  for (const key of candidates) {
    const rows = store.getExportsByModule(key);
    if (rows.length > 0) {
      exports = rows.map((r) => ({ name: r.name }));
      break;
    }
  }
  const available = [...new Set(exports.map((e) => e.name))];
  const found = available.includes(importName);
  if (found) return { found: true };
  const suggestionsWithDist = suggestAlternativesWithDistance(importName, available, 3);
  const suggestions = suggestionsWithDist.map(s => s.name);
  const bestDistance = suggestionsWithDist.length > 0 ? suggestionsWithDist[0].distance : undefined;
  return { found: false, available, suggestions: suggestions.length > 0 ? suggestions : undefined, bestDistance };
}

export function searchSymbolByName(
  store: SymbolStore,
  name: string
): Array<{ file_path: string; module_path: string }> {
  const rows = store.getSymbolByName(name);
  return rows.map((r) => ({ file_path: r.file_path, module_path: r.module_path }));
}

export function searchSymbolsSemantic(
  store: SymbolStore,
  query: string
): Array<{ name: string; kind: string; signature: string | null; file_path: string; usage_count: number }> {
  const rows = store.searchSymbols(query);
  return rows.slice(0, 20).map((s) => ({
    name: s.name,
    kind: s.kind,
    signature: s.signature,
    file_path: s.file_path,
    usage_count: s.usage_count,
  }));
}
