/**
 * Symbol store — SQLite persistence for symbol graph.
 * Prepared statements; FTS5 sync on insert/clearFile.
 */
import Database from 'better-sqlite3';
import { readFileSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

export interface SymbolRow {
  id: number;
  name: string;
  kind: string;
  file_path: string;
  line_number: number | null;
  is_exported: number;
  signature: string | null;
  params: string | null;
  return_type: string | null;
  jsdoc: string | null;
  usage_count: number;
  last_indexed_at: number;
}

export interface SymbolInsertRow extends Omit<SymbolRow, 'id'> {}

export class SymbolStore {
  readonly db: Database.Database;

  private stmtInsertSymbol!: Database.Statement;
  private stmtInsertExport!: Database.Statement;
  private stmtInsertPackage!: Database.Statement;
  private stmtInsertFts!: Database.Statement;
  private stmtGetExportsByModule!: Database.Statement;
  private stmtGetSymbolByName!: Database.Statement;
  private stmtSearchSymbols!: Database.Statement;
  private stmtGetSymbolsByUsage!: Database.Statement;
  private stmtGetPackage!: Database.Statement;
  private stmtGetExportedSymbolsByName!: Database.Statement;
  private stmtInsertReference!: Database.Statement;
  private stmtGetReferencesBySymbol!: Database.Statement;
  private stmtGetReferencesInFile!: Database.Statement;
  private stmtGetSymbolById!: Database.Statement;
  private stmtGetImportsForFile!: Database.Statement;
  private stmtGetImportersOfFile!: Database.Statement;
  private stmtGetDefaultExportId!: Database.Statement;
  private stmtClearFileRefs!: Database.Statement;
  private stmtClearFileSelectIds!: Database.Statement;
  private stmtClearFileDeleteSymbols!: Database.Statement;
  private stmtClearFileDeleteFts!: Database.Statement;
  private stmtLogIntervention!: Database.Statement;
  private stmtGetStats!: Database.Statement;
  private _initialized = false;

  constructor(dbPath: string | ':memory:') {
    this.db = new Database(dbPath);
    this.db.exec('PRAGMA foreign_keys = ON;');
  }

  init(): void {
    const schemaPath = join(__dirname, 'schema.sql');
    const schema = readFileSync(schemaPath, 'utf-8');
    this.db.exec(schema);

    this.stmtInsertSymbol = this.db.prepare(`
      INSERT INTO symbols (name, kind, file_path, line_number, is_exported, signature, params, return_type, jsdoc, usage_count, last_indexed_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `);
    this.stmtInsertExport = this.db.prepare(`
      INSERT INTO exports (symbol_id, module_path, is_default, is_named) VALUES (?, ?, ?, ?)
    `);
    this.stmtInsertPackage = this.db.prepare(`
      INSERT INTO packages (name, version, is_dev_dependency) VALUES (?, ?, ?)
      ON CONFLICT(name) DO UPDATE SET version = excluded.version, is_dev_dependency = excluded.is_dev_dependency
    `);
    this.stmtInsertFts = this.db.prepare(`
      INSERT INTO symbols_fts(rowid, name, file_path, signature, jsdoc) VALUES (?, ?, ?, ?, ?)
    `);
    this.stmtGetExportsByModule = this.db.prepare(`
      SELECT s.id, s.name, s.kind, s.file_path, s.line_number, s.is_exported, s.signature, s.params, s.return_type, s.jsdoc, s.usage_count, s.last_indexed_at
      FROM exports e JOIN symbols s ON e.symbol_id = s.id WHERE e.module_path = ?
    `);
    this.stmtGetSymbolByName = this.db.prepare(`
      SELECT s.id AS symbol_id, s.file_path, e.module_path FROM symbols s
      JOIN exports e ON e.symbol_id = s.id WHERE s.name = ? AND s.is_exported = 1
    `);
    this.stmtSearchSymbols = this.db.prepare(`
      SELECT s.id, s.name, s.kind, s.file_path, s.line_number, s.is_exported, s.signature, s.params, s.return_type, s.jsdoc, s.usage_count, s.last_indexed_at
      FROM symbols_fts fts JOIN symbols s ON s.id = fts.rowid
      WHERE symbols_fts MATCH ? ORDER BY rank LIMIT 50
    `);
    this.stmtGetSymbolsByUsage = this.db.prepare(`
      SELECT id, name, kind, file_path, line_number, is_exported, signature, params, return_type, jsdoc, usage_count, last_indexed_at
      FROM symbols WHERE usage_count >= ? ORDER BY usage_count DESC
    `);
    this.stmtGetPackage = this.db.prepare(`SELECT name, version FROM packages WHERE name = ?`);
    this.stmtGetExportedSymbolsByName = this.db.prepare(`
      SELECT id, name, kind, file_path, line_number, is_exported, signature, params, return_type, jsdoc, usage_count, last_indexed_at
      FROM symbols WHERE name = ? AND is_exported = 1
    `);
    this.stmtInsertReference = this.db.prepare(`
      INSERT INTO "references" (symbol_id, referenced_in_file, referenced_at_line, reference_type) VALUES (?, ?, ?, ?)
    `);
    this.stmtGetReferencesBySymbol = this.db.prepare(`
      SELECT referenced_in_file, referenced_at_line, reference_type FROM "references" WHERE symbol_id = ?
    `);
    this.stmtGetReferencesInFile = this.db.prepare(`
      SELECT symbol_id, referenced_at_line, reference_type FROM "references" WHERE referenced_in_file = ?
    `);
    this.stmtGetSymbolById = this.db.prepare(`
      SELECT id, name, kind, file_path, line_number, is_exported, signature, params, return_type, jsdoc, usage_count, last_indexed_at FROM symbols WHERE id = ?
    `);
    this.stmtGetImportsForFile = this.db.prepare(`
      SELECT r.symbol_id AS symbol_id, s.name AS symbol_name, s.file_path AS symbol_file_path, r.referenced_at_line
      FROM "references" r JOIN symbols s ON r.symbol_id = s.id
      WHERE r.referenced_in_file = ? AND r.reference_type = 'import'
    `);
    this.stmtGetImportersOfFile = this.db.prepare(`
      SELECT DISTINCT r.referenced_in_file FROM "references" r JOIN symbols s ON r.symbol_id = s.id
      WHERE s.file_path = ? AND r.reference_type = 'import'
    `);
    this.stmtGetDefaultExportId = this.db.prepare(`
      SELECT symbol_id FROM exports WHERE module_path = ? AND is_default = 1 LIMIT 1
    `);
    this.stmtClearFileRefs = this.db.prepare(`DELETE FROM "references" WHERE referenced_in_file = ?`);
    this.stmtClearFileSelectIds = this.db.prepare(`SELECT id FROM symbols WHERE file_path = ?`);
    this.stmtClearFileDeleteSymbols = this.db.prepare(`DELETE FROM symbols WHERE file_path = ?`);
    this.stmtClearFileDeleteFts = this.db.prepare(`DELETE FROM symbols_fts WHERE rowid = ?`);
    this.stmtLogIntervention = this.db.prepare(`
      INSERT INTO interventions (timestamp, tool, file_path, phase, outcome, errors_found, errors_fixed, error_types, ai_called, ai_type, latency_ms, fix_accepted)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `);
    this.stmtGetStats = this.db.prepare(`
      SELECT (SELECT COUNT(*) FROM symbols) AS symbolCount, (SELECT COUNT(*) FROM interventions) AS interventionCount, (SELECT COUNT(*) FROM packages) AS packageCount
    `);
    this._initialized = true;
  }

  close(): void {
    this.db.close();
  }

  insertSymbol(row: SymbolInsertRow): number {
    const isExported = row.is_exported ? 1 : 0;
    this.stmtInsertSymbol.run(
      row.name,
      row.kind,
      row.file_path,
      row.line_number ?? null,
      isExported,
      row.signature ?? null,
      row.params ?? null,
      row.return_type ?? null,
      row.jsdoc ?? null,
      row.usage_count ?? 0,
      row.last_indexed_at
    );
    const id = this.db.prepare('SELECT last_insert_rowid() AS id').get() as { id: number };
    this.stmtInsertFts.run(id.id, row.name, row.file_path, row.signature ?? '', row.jsdoc ?? '');
    return id.id;
  }

  insertExport(symbolId: number, modulePath: string, opts?: { isDefault?: boolean; isNamed?: boolean }): void {
    const isDefault = opts?.isDefault ? 1 : 0;
    const isNamed = opts?.isNamed !== false ? 1 : 0;
    this.stmtInsertExport.run(symbolId, modulePath, isDefault, isNamed);
  }

  insertPackage(name: string, version?: string, isDev?: boolean): void {
    this.stmtInsertPackage.run(name, version ?? null, isDev ? 1 : 0);
  }

  getExportsByModule(modulePath: string): SymbolRow[] {
    const rows = this.stmtGetExportsByModule.all(modulePath) as Array<Record<string, unknown>>;
    return rows.map(rowToSymbol);
  }

  getSymbolByName(name: string): Array<{ file_path: string; symbol_id: number; module_path: string }> {
    const rows = this.stmtGetSymbolByName.all(name) as Array<{
      file_path: string;
      symbol_id: number;
      module_path: string;
    }>;
    return rows;
  }

  searchSymbols(query: string): SymbolRow[] {
    const term = query.trim().replace(/\s+/g, ' OR ');
    const prefix = term ? `${term}*` : '';
    if (!prefix) return [];
    let rows: Array<Record<string, unknown>>;
    try {
      rows = this.stmtSearchSymbols.all(prefix) as Array<Record<string, unknown>>;
    } catch {
      rows = this.stmtSearchSymbols.all(term) as Array<Record<string, unknown>>;
    }
    return rows.map(rowToSymbol);
  }

  getSymbolsByUsage(minUsage: number): SymbolRow[] {
    const rows = this.stmtGetSymbolsByUsage.all(minUsage) as Array<Record<string, unknown>>;
    return rows.map(rowToSymbol);
  }

  getPackage(name: string): { name: string; version: string | null } | null {
    const row = this.stmtGetPackage.get(name) as { name: string; version: string | null } | undefined;
    return row ?? null;
  }

  getExportedSymbolsByName(name: string): SymbolRow[] {
    const rows = this.stmtGetExportedSymbolsByName.all(name) as Array<Record<string, unknown>>;
    return rows.map(rowToSymbol);
  }

  insertReference(
    symbolId: number,
    referencedInFile: string,
    referencedAtLine: number | null,
    referenceType: 'import' | 'call' | 'type_usage'
  ): void {
    this.stmtInsertReference.run(symbolId, referencedInFile, referencedAtLine, referenceType);
  }

  getReferences(symbolId: number): Array<{ referenced_in_file: string; referenced_at_line: number | null; reference_type: string }> {
    const rows = this.stmtGetReferencesBySymbol.all(symbolId) as Array<{
      referenced_in_file: string;
      referenced_at_line: number | null;
      reference_type: string;
    }>;
    return rows;
  }

  getReferencesInFile(filePath: string): Array<{ symbol_id: number; referenced_at_line: number | null; reference_type: string }> {
    const rows = this.stmtGetReferencesInFile.all(filePath) as Array<{
      symbol_id: number;
      referenced_at_line: number | null;
      reference_type: string;
    }>;
    return rows;
  }

  getSymbolById(symbolId: number): SymbolRow | null {
    const row = this.stmtGetSymbolById.get(symbolId) as Record<string, unknown> | undefined;
    return row ? rowToSymbol(row) : null;
  }

  getImportsForFile(filePath: string): Array<{ symbol_id: number; symbol_name: string; symbol_file_path: string; referenced_at_line: number | null }> {
    const rows = this.stmtGetImportsForFile.all(filePath) as Array<{
      symbol_id: number;
      symbol_name: string;
      symbol_file_path: string;
      referenced_at_line: number | null;
    }>;
    return rows;
  }

  getImportersOfFile(filePath: string): string[] {
    const rows = this.stmtGetImportersOfFile.all(filePath) as Array<{ referenced_in_file: string }>;
    return rows.map((r) => r.referenced_in_file);
  }

  getDefaultExportIdForModule(modulePath: string): number | null {
    const row = this.stmtGetDefaultExportId.get(modulePath) as { symbol_id: number } | undefined;
    return row?.symbol_id ?? null;
  }

  clearFile(filePath: string): void {
    this.stmtClearFileRefs.run(filePath);
    const ids = (this.stmtClearFileSelectIds.all(filePath) as Array<{ id: number }>).map((r) => r.id);
    this.stmtClearFileDeleteSymbols.run(filePath);
    for (const id of ids) {
      this.stmtClearFileDeleteFts.run(id);
    }
  }

  logIntervention(entry: {
    tool: string;
    file_path: string | null;
    phase: string;
    outcome: string;
    errors_found: number;
    errors_fixed: number;
    error_types: string | null;
    ai_called: boolean;
    ai_type: string | null;
    latency_ms: number | null;
    fix_accepted: boolean | null;
  }): void {
    const timestamp = Math.floor(Date.now() / 1000);
    this.stmtLogIntervention.run(
      timestamp,
      entry.tool,
      entry.file_path,
      entry.phase,
      entry.outcome,
      entry.errors_found,
      entry.errors_fixed,
      entry.error_types,
      entry.ai_called ? 1 : 0,
      entry.ai_type,
      entry.latency_ms,
      entry.fix_accepted === null ? null : entry.fix_accepted ? 1 : 0
    );
  }

  getStats(): { symbolCount: number; interventionCount: number; packageCount: number } {
    const row = this.stmtGetStats.get() as {
      symbolCount: number;
      interventionCount: number;
      packageCount: number;
    };
    return {
      symbolCount: row.symbolCount,
      interventionCount: row.interventionCount,
      packageCount: row.packageCount,
    };
  }
}

function rowToSymbol(row: Record<string, unknown>): SymbolRow {
  return {
    id: row.id as number,
    name: row.name as string,
    kind: row.kind as string,
    file_path: row.file_path as string,
    line_number: row.line_number as number | null,
    is_exported: (row.is_exported as number) ?? 0,
    signature: (row.signature as string) ?? null,
    params: (row.params as string) ?? null,
    return_type: (row.return_type as string) ?? null,
    jsdoc: (row.jsdoc as string) ?? null,
    usage_count: (row.usage_count as number) ?? 0,
    last_indexed_at: row.last_indexed_at as number,
  };
}
