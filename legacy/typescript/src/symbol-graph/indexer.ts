/**
 * Project indexer — ts-morph walk, extract exports, usage counts.
 */
import { Project, type ExportedDeclarations, SyntaxKind } from 'ts-morph';
import { readFileSync, existsSync } from 'fs';
import { join, relative, dirname, normalize } from 'path';
import type { SymbolStore } from './sqlite-store.js';

export interface IndexerStats {
  totalFiles: number;
  totalSymbols: number;
  totalPackages: number;
}

function getKindName(decl: ExportedDeclarations): string {
  const name = (decl as { getKindName?: () => string }).getKindName?.();
  if (!name) return 'symbol';
  const lower = name.toLowerCase();
  if (lower.includes('function')) return 'function';
  if (lower.includes('class')) return 'class';
  if (lower.includes('interface')) return 'interface';
  if (lower.includes('type') && lower.includes('alias')) return 'type';
  if (lower.includes('enum')) return 'enum';
  if (lower.includes('variable') || lower.includes('const') || lower.includes('let')) return 'variable';
  return 'symbol';
}

function getSignature(decl: ExportedDeclarations): string | null {
  const fn = decl as { getSignature?: () => { getText: () => string } };
  return fn.getSignature?.()?.getText?.() ?? null;
}

function getParams(decl: ExportedDeclarations): Array<{ name: string; type: string }> | null {
  const fn = decl as { getParameters?: () => Array<{ getName: () => string; getType: () => { getText: () => string } }> };
  const params = fn.getParameters?.();
  if (!params?.length) return null;
  return params.map((p) => ({ name: p.getName(), type: p.getType?.()?.getText?.() ?? 'unknown' }));
}

function getReturnType(decl: ExportedDeclarations): string | null {
  const fn = decl as { getReturnType?: () => { getText: () => string } };
  return fn.getReturnType?.()?.getText?.() ?? null;
}

function buildSignatureString(
  params: Array<{ name: string; type: string }> | null,
  returnType: string | null
): string | null {
  if (!params || params.length === 0) return null;
  const paramStr = params.map(p => `${p.name}: ${p.type}`).join(', ');
  return returnType ? `(${paramStr}) => ${returnType}` : `(${paramStr})`;
}

function getJsdoc(decl: ExportedDeclarations): string | null {
  const d = decl as { getJsDoc?: () => Array<{ getComment: () => string }> };
  const docs = d.getJsDoc?.();
  if (!docs?.length) return null;
  return docs.map((j) => j.getComment?.() ?? '').join('\n').trim() || null;
}

export class ProjectIndexer {
  private fileCount = 0;
  private symbolCount = 0;

  constructor(private store: SymbolStore) {}

  async indexProject(projectPath: string): Promise<IndexerStats> {
    this.fileCount = 0;
    this.symbolCount = 0;
    const tsconfigPath = join(projectPath, 'tsconfig.json');
    const project = existsSync(tsconfigPath)
      ? new Project({ tsConfigFilePath: tsconfigPath })
      : new Project({ compilerOptions: { strict: true } });
    if (!existsSync(tsconfigPath)) {
      project.addSourceFilesAtPaths(join(projectPath, '**/*.ts'));
      project.addSourceFilesAtPaths(join(projectPath, '**/*.tsx'));
    }
    const sourceFiles = project.getSourceFiles();
    const usageMap = new Map<string, number>();
    for (const file of sourceFiles) {
      const filePath = file.getFilePath();
      const rel = relative(projectPath, filePath).replace(/\\/g, '/');
      if (rel.startsWith('..') || rel.includes('node_modules')) continue;
      for (const imp of file.getImportDeclarations()) {
        const spec = imp.getModuleSpecifierValue();
        if (!spec) continue;
        const fromDir = dirname(filePath);
        const resolved = normalize(join(fromDir, spec)).replace(/\\/g, '/');
        let moduleKey: string;
        if (spec.startsWith('.') || spec.startsWith('/')) {
          moduleKey = relative(projectPath, resolved).replace(/\\/g, '/').replace(/\.(ts|tsx|js|jsx)$/i, '');
        } else {
          moduleKey = spec;
        }
        const def = imp.getDefaultImport();
        if (def) {
          const name = def.getText();
          const key = `${moduleKey}\0${name}`;
          usageMap.set(key, (usageMap.get(key) ?? 0) + 1);
        }
        for (const named of imp.getNamedImports()) {
          const name = named.getName();
          const key = `${moduleKey}\0${name}`;
          usageMap.set(key, (usageMap.get(key) ?? 0) + 1);
        }
        if (imp.getNamespaceImport()) {
          const name = imp.getNamespaceImport()!.getText();
          const key = `${moduleKey}\0${name}`;
          usageMap.set(key, (usageMap.get(key) ?? 0) + 1);
        }
      }
    }

    const now = Math.floor(Date.now() / 1000);
    for (const file of sourceFiles) {
      const filePath = file.getFilePath();
      const rel = relative(projectPath, filePath).replace(/\\/g, '/');
      if (rel.startsWith('..') || rel.includes('node_modules')) continue;
      const modulePath = rel.replace(/\.(ts|tsx|js|jsx)$/i, '');
      this.store.clearFile(rel);
      const exported = file.getExportedDeclarations();
      for (const [exportName, decls] of exported) {
        const decl = decls[0];
        if (!decl) continue;
        const kind = getKindName(decl);
        const params = getParams(decl);
        const returnType = getReturnType(decl);
        const signature = getSignature(decl) ?? buildSignatureString(params, returnType);
        const jsdoc = getJsdoc(decl);
        const line = decl.getStartLineNumber?.() ?? null;
        const usageKey = `${modulePath}\0${exportName}`;
        const usage_count = usageMap.get(usageKey) ?? 0;
        const isDefault = exportName === 'default';
        const symbolName = isDefault
          ? ((decl as { getName?: () => string }).getName?.() ?? 'default')
          : exportName;
        const symbolId = this.store.insertSymbol({
          name: symbolName,
          kind,
          file_path: rel,
          line_number: line,
          is_exported: 1,
          signature,
          params: params ? JSON.stringify(params) : null,
          return_type: returnType,
          jsdoc,
          usage_count,
          last_indexed_at: now,
        });
        this.store.insertExport(symbolId, modulePath, { isDefault: isDefault, isNamed: !isDefault });
        this.symbolCount++;
      }
      if (exported.size > 0) this.fileCount++;
    }

    const pkgPath = join(projectPath, 'package.json');
    if (existsSync(pkgPath)) {
      const pkg = JSON.parse(readFileSync(pkgPath, 'utf-8')) as {
        dependencies?: Record<string, string>;
        devDependencies?: Record<string, string>;
      };
      const deps = pkg.dependencies ?? {};
      const devDeps = pkg.devDependencies ?? {};
      for (const [name, version] of Object.entries(deps)) {
        this.store.insertPackage(name, typeof version === 'string' ? version : undefined, false);
      }
      for (const [name, version] of Object.entries(devDeps)) {
        this.store.insertPackage(name, typeof version === 'string' ? version : undefined, true);
      }
    }

    // Second pass: record references (imports and call sites)
    for (const file of sourceFiles) {
      const filePath = file.getFilePath();
      const rel = relative(projectPath, filePath).replace(/\\/g, '/');
      if (rel.startsWith('..') || rel.includes('node_modules')) continue;
      const fromDir = dirname(filePath);
      const importNameToSymbolId = new Map<string, number>();

      for (const imp of file.getImportDeclarations()) {
        const spec = imp.getModuleSpecifierValue();
        if (!spec) continue;
        const resolved = normalize(join(fromDir, spec)).replace(/\\/g, '/');
        let modulePath: string;
        if (spec.startsWith('.') || spec.startsWith('/')) {
          const relPath = relative(projectPath, resolved).replace(/\\/g, '/');
          modulePath = relPath.replace(/\.(ts|tsx|js|jsx)$/i, '');
        } else {
          modulePath = spec;
        }
        const line = imp.getStartLineNumber?.() ?? null;

        const def = imp.getDefaultImport();
        if (def) {
          const name = def.getText();
          const symbolId = this.store.getDefaultExportIdForModule(modulePath);
          if (symbolId != null) {
            this.store.insertReference(symbolId, rel, line, 'import');
            importNameToSymbolId.set(name, symbolId);
          }
        }
        for (const named of imp.getNamedImports()) {
          const name = named.getName();
          const exports = this.store.getExportsByModule(modulePath);
          const sym = exports.find((s) => s.name === name);
          if (sym) {
            this.store.insertReference(sym.id, rel, line, 'import');
            importNameToSymbolId.set(name, sym.id);
          }
        }
        const ns = imp.getNamespaceImport();
        if (ns) {
          const name = ns.getText();
          const defaultId = this.store.getDefaultExportIdForModule(modulePath);
          if (defaultId != null) {
            this.store.insertReference(defaultId, rel, line, 'import');
            importNameToSymbolId.set(name, defaultId);
          }
        }
      }

      // Call sites: identifiers that are called and match an imported symbol
      const callExpressions = file.getDescendantsOfKind(SyntaxKind.CallExpression);
      for (const call of callExpressions) {
        const expr = call.getExpression();
        const name = expr.getText();
        const symbolId = importNameToSymbolId.get(name);
        if (symbolId != null) {
          const line = call.getStartLineNumber?.() ?? null;
          this.store.insertReference(symbolId, rel, line, 'call');
        }
      }
    }

    const stats = this.store.getStats();
    return {
      totalFiles: this.fileCount,
      totalSymbols: this.symbolCount,
      totalPackages: stats.packageCount,
    };
  }

  indexFile(filePath: string): void {
    const projectPath = dirname(filePath);
    const project = new Project({ compilerOptions: { strict: true } });
    const file = project.addSourceFileAtPath(filePath);
    const rel = relative(projectPath, filePath).replace(/\\/g, '/');
    const modulePath = rel.replace(/\.(ts|tsx|js|jsx)$/i, '');
    this.store.clearFile(rel);
    const now = Math.floor(Date.now() / 1000);
    const exported = file.getExportedDeclarations();
    for (const [exportName, decls] of exported) {
      const decl = decls[0];
      if (!decl) continue;
      const kind = getKindName(decl);
      const signature = getSignature(decl);
      const params = getParams(decl);
      const returnType = getReturnType(decl);
      const jsdoc = getJsdoc(decl);
      const line = decl.getStartLineNumber?.() ?? null;
      const symbolId = this.store.insertSymbol({
        name: exportName,
        kind,
        file_path: rel,
        line_number: line,
        is_exported: 1,
        signature,
        params: params ? JSON.stringify(params) : null,
        return_type: returnType,
        jsdoc,
        usage_count: 0,
        last_indexed_at: now,
      });
      this.store.insertExport(symbolId, modulePath, { isDefault: false, isNamed: true });
      this.symbolCount++;
    }
  }

  getStats(): IndexerStats {
    const stats = this.store.getStats();
    return {
      totalFiles: this.fileCount,
      totalSymbols: stats.symbolCount,
      totalPackages: stats.packageCount,
    };
  }
}
