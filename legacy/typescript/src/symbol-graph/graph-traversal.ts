/**
 * Graph traversal — import/call graph walking (CLAUDE spec).
 * Pure deterministic, no AI. Implementation uses references table.
 */
import type { SymbolStore } from './sqlite-store.js';

export interface FileNode {
  path: string;
  distance: number;
}

export interface Reference {
  referenced_in_file: string;
  referenced_at_line: number | null;
  reference_type: string;
}

function normalizeFilePath(path: string): string {
  const p = path.replace(/\\/g, '/');
  if (/\.(ts|tsx|js|jsx)$/i.test(p)) return p;
  return `${p}.ts`;
}

export class GraphTraversal {
  constructor(private store: SymbolStore) {}

  /** Given entry file(s), find all connected files via import relationships. BFS over import graph. */
  findConnectedFiles(entryFiles: string[], maxDepth: number): FileNode[] {
    const normalized = entryFiles.map((f) => normalizeFilePath(f));
    const visited = new Set<string>(normalized);
    const result: FileNode[] = normalized.map((path) => ({ path, distance: 0 }));
    const queue: Array<{ path: string; distance: number }> = result.map((n) => ({ path: n.path, distance: n.distance }));

    while (queue.length > 0) {
      const { path, distance } = queue.shift()!;
      if (distance >= maxDepth) continue;

      const nextDist = distance + 1;
      const importers = this.store.getImportersOfFile(path);
      for (const imp of importers) {
        const p = normalizeFilePath(imp);
        if (!visited.has(p)) {
          visited.add(p);
          result.push({ path: p, distance: nextDist });
          queue.push({ path: p, distance: nextDist });
        }
      }
      const imports = this.store.getImportsForFile(path);
      for (const imp of imports) {
        const p = normalizeFilePath(imp.symbol_file_path);
        if (!visited.has(p)) {
          visited.add(p);
          result.push({ path: p, distance: nextDist });
          queue.push({ path: p, distance: nextDist });
        }
      }
    }
    return result;
  }

  /** Given a symbol, find all files that reference it (callers). */
  findCallers(symbolName: string): Reference[] {
    const rows = this.store.getSymbolByName(symbolName);
    const refs: Reference[] = [];
    for (const row of rows) {
      const refList = this.store.getReferences(row.symbol_id);
      refs.push(...refList.map((r) => ({ referenced_in_file: r.referenced_in_file, referenced_at_line: r.referenced_at_line, reference_type: r.reference_type })));
    }
    return refs;
  }

  /** Given a file, find all symbols it references (callees): imports + call targets. */
  findCallees(_symbolName: string, filePath: string): Reference[] {
    const path = normalizeFilePath(filePath);
    const refs: Reference[] = [];
    const imports = this.store.getImportsForFile(path);
    for (const imp of imports) {
      refs.push({
        referenced_in_file: imp.symbol_file_path,
        referenced_at_line: imp.referenced_at_line,
        reference_type: 'import',
      });
    }
    const inFile = this.store.getReferencesInFile(path);
    for (const r of inFile) {
      if (r.reference_type === 'call') {
        const sym = this.store.getSymbolById(r.symbol_id);
        if (sym) {
          refs.push({
            referenced_in_file: sym.file_path,
            referenced_at_line: r.referenced_at_line,
            reference_type: 'call',
          });
        }
      }
    }
    return refs;
  }

  /** Full impact analysis: if this symbol changes, what breaks? */
  getImpactRadius(symbolName: string): { files: string[]; totalReferences: number } {
    const callers = this.findCallers(symbolName);
    const filesSet = new Set<string>();
    const rows = this.store.getSymbolByName(symbolName);
    for (const row of rows) {
      filesSet.add(normalizeFilePath(row.file_path));
    }
    for (const r of callers) {
      filesSet.add(normalizeFilePath(r.referenced_in_file));
    }
    return {
      files: [...filesSet],
      totalReferences: callers.length,
    };
  }
}
