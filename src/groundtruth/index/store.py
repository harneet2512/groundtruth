"""SQLite-backed symbol store with CRUD, FTS5, and graph queries."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from groundtruth.utils.platform import normalize_path, paths_equal
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@dataclass
class SymbolRecord:
    """A symbol from the database."""

    id: int
    name: str
    kind: str
    language: str
    file_path: str
    line_number: int | None
    end_line: int | None
    is_exported: bool
    signature: str | None
    params: str | None
    return_type: str | None
    documentation: str | None
    usage_count: int
    last_indexed_at: int


@dataclass
class RefRecord:
    """A reference from the database."""

    id: int
    symbol_id: int
    referenced_in_file: str
    referenced_at_line: int | None
    reference_type: str


@dataclass
class ExportRecord:
    """An export from the database."""

    id: int
    symbol_id: int
    module_path: str
    is_default: bool
    is_named: bool


@dataclass
class PackageRecord:
    """A package from the database."""

    id: int
    name: str
    version: str | None
    package_manager: str
    is_dev_dependency: bool


@dataclass
class BriefingLogRecord:
    """A briefing log entry from the database."""

    id: int
    timestamp: int
    intent: str
    briefing_text: str
    briefing_symbols: list[str]
    target_file: str | None
    subsequent_validation_id: int | None
    compliance_rate: float | None
    symbols_used_correctly: list[str] | None
    symbols_ignored: list[str] | None
    hallucinated_despite_briefing: list[str] | None


def _parse_json_list(val: str | None) -> list[str] | None:
    """Parse a JSON string into a list of strings, or return None."""
    if val is None:
        return None
    try:
        result = json.loads(val)
        if isinstance(result, list):
            return [str(x) for x in result]
        return None
    except (json.JSONDecodeError, TypeError):
        return None


def _row_to_briefing_log(row: sqlite3.Row) -> BriefingLogRecord:
    """Convert a sqlite3.Row to a BriefingLogRecord."""
    return BriefingLogRecord(
        id=row["id"],
        timestamp=row["timestamp"],
        intent=row["intent"],
        briefing_text=row["briefing_text"],
        briefing_symbols=_parse_json_list(row["briefing_symbols"]) or [],
        target_file=row["target_file"],
        subsequent_validation_id=row["subsequent_validation_id"],
        compliance_rate=row["compliance_rate"],
        symbols_used_correctly=_parse_json_list(row["symbols_used_correctly"]),
        symbols_ignored=_parse_json_list(row["symbols_ignored"]),
        hallucinated_despite_briefing=_parse_json_list(row["hallucinated_despite_briefing"]),
    )


def _row_to_symbol(row: sqlite3.Row) -> SymbolRecord:
    """Convert a sqlite3.Row to a SymbolRecord."""
    return SymbolRecord(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        language=row["language"],
        file_path=row["file_path"],
        line_number=row["line_number"],
        end_line=row["end_line"],
        is_exported=bool(row["is_exported"]),
        signature=row["signature"],
        params=row["params"],
        return_type=row["return_type"],
        documentation=row["documentation"],
        usage_count=row["usage_count"],
        last_indexed_at=row["last_indexed_at"],
    )


class SymbolStore:
    """SQLite-backed storage for the symbol index."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> Result[None, GroundTruthError]:
        """Open connection and create schema."""
        try:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            schema = SCHEMA_PATH.read_text()
            self._conn.executescript(schema)
            # Migration: add run_id to interventions if missing (existing DBs)
            try:
                self._conn.execute("ALTER TABLE interventions ADD COLUMN run_id TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            return Ok(None)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_init_failed",
                    message=f"Failed to initialize database: {exc}",
                )
            )

    @property
    def connection(self) -> sqlite3.Connection:
        """Get the active database connection."""
        assert self._conn is not None, "Store not initialized. Call initialize() first."
        return self._conn

    # --- Symbol Operations ---

    def insert_symbol(
        self,
        name: str,
        kind: str,
        language: str,
        file_path: str,
        line_number: int | None,
        end_line: int | None,
        is_exported: bool,
        signature: str | None,
        params: str | None,
        return_type: str | None,
        documentation: str | None,
        last_indexed_at: int,
    ) -> Result[int, GroundTruthError]:
        """Insert a symbol and return its ID. Also inserts into FTS5."""
        try:
            cursor = self.connection.execute(
                """INSERT INTO symbols
                   (name, kind, language, file_path, line_number, end_line,
                    is_exported, signature, params, return_type, documentation, last_indexed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    kind,
                    language,
                    file_path,
                    line_number,
                    end_line,
                    is_exported,
                    signature,
                    params,
                    return_type,
                    documentation,
                    last_indexed_at,
                ),
            )
            symbol_id = cursor.lastrowid
            assert symbol_id is not None
            try:
                self.connection.execute(
                    "INSERT INTO symbols_fts (rowid, name, file_path, signature, documentation) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (symbol_id, name, file_path, signature, documentation),
                )
            except sqlite3.IntegrityError:
                self.rebuild_fts()
                self.connection.execute(
                    "INSERT INTO symbols_fts (rowid, name, file_path, signature, documentation) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (symbol_id, name, file_path, signature, documentation),
                )
            self.connection.commit()
            return Ok(symbol_id)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_insert_failed",
                    message=f"Failed to insert symbol: {exc}",
                )
            )

    def find_symbol_by_name(self, name: str) -> Result[list[SymbolRecord], GroundTruthError]:
        """Find symbols by exact name."""
        try:
            cursor = self.connection.execute("SELECT * FROM symbols WHERE name = ?", (name,))
            return Ok([_row_to_symbol(row) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to find symbol: {exc}",
                )
            )

    def get_symbols_in_file(self, file_path: str) -> Result[list[SymbolRecord], GroundTruthError]:
        """Get all symbols in a file."""
        try:
            cursor = self.connection.execute(
                "SELECT * FROM symbols WHERE file_path = ?", (file_path,)
            )
            return Ok([_row_to_symbol(row) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get symbols in file: {exc}",
                )
            )

    def delete_symbols_in_file(self, file_path: str) -> Result[int, GroundTruthError]:
        """Delete all symbols for a file (before re-indexing). Returns count deleted."""
        try:
            # Get IDs for FTS cleanup
            cursor = self.connection.execute(
                "SELECT id FROM symbols WHERE file_path = ?", (file_path,)
            )
            ids = [row["id"] for row in cursor.fetchall()]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.connection.execute(
                    f"DELETE FROM symbols_fts WHERE rowid IN ({placeholders})",  # noqa: S608
                    ids,
                )
                self.connection.execute(
                    f"DELETE FROM symbols WHERE id IN ({placeholders})",  # noqa: S608
                    ids,
                )
                self.connection.commit()
            return Ok(len(ids))
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_delete_failed",
                    message=f"Failed to delete symbols: {exc}",
                )
            )

    def get_symbol_by_id(self, symbol_id: int) -> Result[SymbolRecord | None, GroundTruthError]:
        """Look up a symbol by its primary key."""
        try:
            cursor = self.connection.execute("SELECT * FROM symbols WHERE id = ?", (symbol_id,))
            row = cursor.fetchone()
            if row is None:
                return Ok(None)
            return Ok(_row_to_symbol(row))
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get symbol by id: {exc}",
                )
            )

    def get_refs_from_file(
        self, file_path: str, reference_type: str | None = None
    ) -> Result[list[RefRecord], GroundTruthError]:
        """Get all refs originating from a file, optionally filtered by type."""
        try:
            if reference_type is not None:
                cursor = self.connection.execute(
                    "SELECT * FROM refs WHERE referenced_in_file = ? AND reference_type = ?",
                    (file_path, reference_type),
                )
            else:
                cursor = self.connection.execute(
                    "SELECT * FROM refs WHERE referenced_in_file = ?",
                    (file_path,),
                )
            return Ok(
                [
                    RefRecord(
                        id=row["id"],
                        symbol_id=row["symbol_id"],
                        referenced_in_file=row["referenced_in_file"],
                        referenced_at_line=row["referenced_at_line"],
                        reference_type=row["reference_type"],
                    )
                    for row in cursor.fetchall()
                ]
            )
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get refs from file: {exc}",
                )
            )

    def get_all_symbol_names(self) -> Result[list[str], GroundTruthError]:
        """Get all unique symbol names."""
        try:
            cursor = self.connection.execute("SELECT DISTINCT name FROM symbols")
            return Ok([row["name"] for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get symbol names: {exc}",
                )
            )

    def get_all_files(self) -> Result[list[str], GroundTruthError]:
        """Get all distinct file paths in the index."""
        try:
            cursor = self.connection.execute("SELECT DISTINCT file_path FROM symbols")
            return Ok([row["file_path"] for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get file paths: {exc}",
                )
            )

    def update_usage_count(self, symbol_id: int, count: int) -> Result[None, GroundTruthError]:
        """Update the usage count for a symbol."""
        try:
            self.connection.execute(
                "UPDATE symbols SET usage_count = ? WHERE id = ?", (count, symbol_id)
            )
            self.connection.commit()
            return Ok(None)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_update_failed",
                    message=f"Failed to update usage count: {exc}",
                )
            )

    # --- Export Operations ---

    def insert_export(
        self,
        symbol_id: int,
        module_path: str,
        is_default: bool = False,
        is_named: bool = True,
    ) -> Result[int, GroundTruthError]:
        """Insert an export record and return its ID."""
        try:
            cursor = self.connection.execute(
                "INSERT INTO exports (symbol_id, module_path, is_default, is_named) "
                "VALUES (?, ?, ?, ?)",
                (symbol_id, module_path, is_default, is_named),
            )
            self.connection.commit()
            export_id = cursor.lastrowid
            assert export_id is not None
            return Ok(export_id)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_insert_failed",
                    message=f"Failed to insert export: {exc}",
                )
            )

    def get_exports_by_module(
        self, module_path: str
    ) -> Result[list[SymbolRecord], GroundTruthError]:
        """Get all symbols exported from a module path."""
        try:
            cursor = self.connection.execute(
                """SELECT s.* FROM symbols s
                   JOIN exports e ON s.id = e.symbol_id
                   WHERE e.module_path = ?""",
                (module_path,),
            )
            return Ok([_row_to_symbol(row) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get exports: {exc}",
                )
            )

    # --- Reference Operations ---

    def insert_ref(
        self,
        symbol_id: int,
        referenced_in_file: str,
        referenced_at_line: int | None,
        reference_type: str,
    ) -> Result[int, GroundTruthError]:
        """Insert a reference and return its ID."""
        try:
            cursor = self.connection.execute(
                "INSERT INTO refs (symbol_id, referenced_in_file, referenced_at_line, reference_type) "
                "VALUES (?, ?, ?, ?)",
                (symbol_id, referenced_in_file, referenced_at_line, reference_type),
            )
            self.connection.commit()
            ref_id = cursor.lastrowid
            assert ref_id is not None
            return Ok(ref_id)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_insert_failed",
                    message=f"Failed to insert ref: {exc}",
                )
            )

    def get_refs_for_symbol(self, symbol_id: int) -> Result[list[RefRecord], GroundTruthError]:
        """Get all references for a symbol."""
        try:
            cursor = self.connection.execute("SELECT * FROM refs WHERE symbol_id = ?", (symbol_id,))
            return Ok(
                [
                    RefRecord(
                        id=row["id"],
                        symbol_id=row["symbol_id"],
                        referenced_in_file=row["referenced_in_file"],
                        referenced_at_line=row["referenced_at_line"],
                        reference_type=row["reference_type"],
                    )
                    for row in cursor.fetchall()
                ]
            )
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get refs: {exc}",
                )
            )

    def get_imports_for_file(self, file_path: str) -> Result[list[RefRecord], GroundTruthError]:
        """Get all references originating FROM a file (what does this file import?)."""
        try:
            cursor = self.connection.execute(
                "SELECT * FROM refs WHERE referenced_in_file = ? AND reference_type = 'import'",
                (file_path,),
            )
            return Ok(
                [
                    RefRecord(
                        id=row["id"],
                        symbol_id=row["symbol_id"],
                        referenced_in_file=row["referenced_in_file"],
                        referenced_at_line=row["referenced_at_line"],
                        reference_type=row["reference_type"],
                    )
                    for row in cursor.fetchall()
                ]
            )
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get imports for file: {exc}",
                )
            )

    def get_importers_of_file(self, file_path: str) -> Result[list[str], GroundTruthError]:
        """Get files that reference symbols defined IN this file."""
        try:
            cursor = self.connection.execute(
                """SELECT DISTINCT r.referenced_in_file
                   FROM refs r
                   JOIN symbols s ON r.symbol_id = s.id
                   WHERE s.file_path = ?""",
                (file_path,),
            )
            return Ok([row["referenced_in_file"] for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get importers: {exc}",
                )
            )

    # --- Package Operations ---

    def insert_package(
        self,
        name: str,
        version: str | None,
        package_manager: str,
        is_dev_dependency: bool = False,
    ) -> Result[int, GroundTruthError]:
        """Insert a package (INSERT OR IGNORE for unique constraint)."""
        try:
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO packages
                   (name, version, package_manager, is_dev_dependency)
                   VALUES (?, ?, ?, ?)""",
                (name, version, package_manager, is_dev_dependency),
            )
            self.connection.commit()
            pkg_id = cursor.lastrowid
            assert pkg_id is not None
            return Ok(pkg_id)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_insert_failed",
                    message=f"Failed to insert package: {exc}",
                )
            )

    def get_package(self, name: str) -> Result[PackageRecord | None, GroundTruthError]:
        """Look up a package by name."""
        try:
            cursor = self.connection.execute("SELECT * FROM packages WHERE name = ?", (name,))
            row = cursor.fetchone()
            if row is None:
                return Ok(None)
            return Ok(
                PackageRecord(
                    id=row["id"],
                    name=row["name"],
                    version=row["version"],
                    package_manager=row["package_manager"],
                    is_dev_dependency=bool(row["is_dev_dependency"]),
                )
            )
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get package: {exc}",
                )
            )

    def get_all_packages(self) -> Result[list[PackageRecord], GroundTruthError]:
        """Get all packages."""
        try:
            cursor = self.connection.execute("SELECT * FROM packages")
            return Ok(
                [
                    PackageRecord(
                        id=row["id"],
                        name=row["name"],
                        version=row["version"],
                        package_manager=row["package_manager"],
                        is_dev_dependency=bool(row["is_dev_dependency"]),
                    )
                    for row in cursor.fetchall()
                ]
            )
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get packages: {exc}",
                )
            )

    def get_file_dependencies(
        self, max_deps: int = 5000
    ) -> Result[list[tuple[str, str, str]], GroundTruthError]:
        """Get cross-file dependencies from refs table."""
        try:
            cursor = self.connection.execute(
                """SELECT DISTINCT r.referenced_in_file AS source,
                          s.file_path AS target,
                          r.reference_type AS type
                   FROM refs r
                   JOIN symbols s ON r.symbol_id = s.id
                   WHERE r.referenced_in_file != s.file_path
                   LIMIT ?""",
                (max_deps,),
            )
            return Ok([(row["source"], row["target"], row["type"]) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get file dependencies: {exc}",
                )
            )

    # --- FTS5 Search ---

    def search_symbols_fts(
        self, query: str, limit: int = 20
    ) -> Result[list[SymbolRecord], GroundTruthError]:
        """Full-text search over symbols."""
        try:
            cursor = self.connection.execute(
                """SELECT s.* FROM symbols s
                   JOIN symbols_fts fts ON s.id = fts.rowid
                   WHERE symbols_fts MATCH ?
                   LIMIT ?""",
                (query, limit),
            )
            return Ok([_row_to_symbol(row) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"FTS search failed: {exc}",
                )
            )

    # --- Intervention Logging ---

    def log_intervention(
        self,
        tool: str,
        phase: str,
        outcome: str,
        file_path: str | None = None,
        language: str | None = None,
        errors_found: int = 0,
        errors_fixed: int = 0,
        error_types: str | None = None,
        ai_called: bool = False,
        ai_model: str | None = None,
        latency_ms: int | None = None,
        tokens_used: int = 0,
        fix_accepted: bool | None = None,
        run_id: str | None = None,
    ) -> Result[int, GroundTruthError]:
        """Log an intervention event."""
        run_id_val = run_id or os.environ.get("GROUNDTRUTH_RUN_ID")
        try:
            cursor = self.connection.execute(
                """INSERT INTO interventions
                   (timestamp, tool, file_path, language, phase, outcome,
                    errors_found, errors_fixed, error_types, ai_called,
                    ai_model, latency_ms, tokens_used, fix_accepted, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(time.time()),
                    tool,
                    file_path,
                    language,
                    phase,
                    outcome,
                    errors_found,
                    errors_fixed,
                    error_types,
                    ai_called,
                    ai_model,
                    latency_ms,
                    tokens_used,
                    fix_accepted,
                    run_id_val,
                ),
            )
            self.connection.commit()
            intervention_id = cursor.lastrowid
            assert intervention_id is not None
            return Ok(intervention_id)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_insert_failed",
                    message=f"Failed to log intervention: {exc}",
                )
            )

    def get_stats(self) -> Result[dict[str, object], GroundTruthError]:
        """Get aggregate intervention statistics."""
        try:
            stats: dict[str, object] = {}
            cursor = self.connection.execute("SELECT COUNT(*) as cnt FROM symbols")
            row = cursor.fetchone()
            stats["symbols_count"] = row["cnt"] if row else 0

            cursor = self.connection.execute("SELECT COUNT(DISTINCT file_path) as cnt FROM symbols")
            row = cursor.fetchone()
            stats["files_count"] = row["cnt"] if row else 0

            cursor = self.connection.execute("SELECT COUNT(*) as cnt FROM refs")
            row = cursor.fetchone()
            stats["refs_count"] = row["cnt"] if row else 0

            cursor = self.connection.execute("SELECT COUNT(*) as cnt FROM interventions")
            row = cursor.fetchone()
            stats["total_interventions"] = row["cnt"] if row else 0

            cursor = self.connection.execute(
                "SELECT COUNT(*) as cnt FROM interventions WHERE outcome != 'valid'"
            )
            row = cursor.fetchone()
            stats["hallucinations_caught"] = row["cnt"] if row else 0

            cursor = self.connection.execute(
                "SELECT COUNT(*) as cnt FROM interventions WHERE ai_called = TRUE"
            )
            row = cursor.fetchone()
            stats["ai_calls"] = row["cnt"] if row else 0

            cursor = self.connection.execute(
                "SELECT COALESCE(SUM(tokens_used), 0) as total FROM interventions"
            )
            row = cursor.fetchone()
            stats["tokens_used"] = row["total"] if row else 0

            return Ok(stats)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get stats: {exc}",
                )
            )

    def get_dead_code(self) -> Result[list[SymbolRecord], GroundTruthError]:
        """Get exported symbols with zero references (dead code)."""
        try:
            cursor = self.connection.execute(
                """SELECT s.* FROM symbols s
                   WHERE s.is_exported = TRUE AND s.usage_count = 0
                   ORDER BY s.file_path, s.name"""
            )
            return Ok([_row_to_symbol(row) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get dead code: {exc}",
                )
            )

    def get_unused_packages(self) -> Result[list[PackageRecord], GroundTruthError]:
        """Get packages that no file imports."""
        try:
            cursor = self.connection.execute(
                """SELECT p.* FROM packages p
                   WHERE NOT EXISTS (
                       SELECT 1 FROM refs r
                       JOIN symbols s ON r.symbol_id = s.id
                       WHERE r.reference_type = 'import'
                       AND s.name = p.name
                   )
                   ORDER BY p.name"""
            )
            return Ok(
                [
                    PackageRecord(
                        id=row["id"],
                        name=row["name"],
                        version=row["version"],
                        package_manager=row["package_manager"],
                        is_dev_dependency=bool(row["is_dev_dependency"]),
                    )
                    for row in cursor.fetchall()
                ]
            )
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get unused packages: {exc}",
                )
            )

    def get_hotspots(self, limit: int = 20) -> Result[list[SymbolRecord], GroundTruthError]:
        """Get the most-referenced symbols in the codebase."""
        try:
            cursor = self.connection.execute(
                """SELECT s.* FROM symbols s
                   WHERE s.usage_count > 0
                   ORDER BY s.usage_count DESC
                   LIMIT ?""",
                (limit,),
            )
            return Ok([_row_to_symbol(row) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get hotspots: {exc}",
                )
            )

    # --- Briefing Log Operations ---

    def insert_briefing_log(
        self,
        timestamp: int,
        intent: str,
        briefing_text: str,
        briefing_symbols: list[str],
        target_file: str | None = None,
    ) -> Result[int, GroundTruthError]:
        """Insert a briefing log entry and return its ID."""
        try:
            cursor = self.connection.execute(
                """INSERT INTO briefing_logs
                   (timestamp, intent, briefing_text, briefing_symbols, target_file)
                   VALUES (?, ?, ?, ?, ?)""",
                (timestamp, intent, briefing_text, json.dumps(briefing_symbols), target_file),
            )
            self.connection.commit()
            log_id = cursor.lastrowid
            assert log_id is not None
            return Ok(log_id)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_insert_failed",
                    message=f"Failed to insert briefing log: {exc}",
                )
            )

    def get_briefing_log(self, log_id: int) -> Result[BriefingLogRecord | None, GroundTruthError]:
        """Look up a briefing log by its primary key."""
        try:
            cursor = self.connection.execute("SELECT * FROM briefing_logs WHERE id = ?", (log_id,))
            row = cursor.fetchone()
            if row is None:
                return Ok(None)
            return Ok(_row_to_briefing_log(row))
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get briefing log: {exc}",
                )
            )

    def link_briefing_to_validation(
        self, log_id: int, validation_id: int
    ) -> Result[None, GroundTruthError]:
        """Link a briefing log to a subsequent validation intervention."""
        try:
            self.connection.execute(
                "UPDATE briefing_logs SET subsequent_validation_id = ? WHERE id = ?",
                (validation_id, log_id),
            )
            self.connection.commit()
            return Ok(None)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_update_failed",
                    message=f"Failed to link briefing to validation: {exc}",
                )
            )

    def update_briefing_compliance(
        self,
        log_id: int,
        compliance_rate: float,
        symbols_used_correctly: list[str],
        symbols_ignored: list[str],
        hallucinated_despite_briefing: list[str],
    ) -> Result[None, GroundTruthError]:
        """Update compliance data on a briefing log."""
        try:
            self.connection.execute(
                """UPDATE briefing_logs
                   SET compliance_rate = ?,
                       symbols_used_correctly = ?,
                       symbols_ignored = ?,
                       hallucinated_despite_briefing = ?
                   WHERE id = ?""",
                (
                    compliance_rate,
                    json.dumps(symbols_used_correctly),
                    json.dumps(symbols_ignored),
                    json.dumps(hallucinated_despite_briefing),
                    log_id,
                ),
            )
            self.connection.commit()
            return Ok(None)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_update_failed",
                    message=f"Failed to update briefing compliance: {exc}",
                )
            )

    def get_recent_briefing_logs(
        self, limit: int = 50
    ) -> Result[list[BriefingLogRecord], GroundTruthError]:
        """Get the most recent briefing logs."""
        try:
            cursor = self.connection.execute(
                "SELECT * FROM briefing_logs ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return Ok([_row_to_briefing_log(row) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get recent briefing logs: {exc}",
                )
            )

    def get_briefing_logs_for_file(
        self, file_path: str
    ) -> Result[list[BriefingLogRecord], GroundTruthError]:
        """Get briefing logs targeting a specific file."""
        try:
            cursor = self.connection.execute(
                "SELECT * FROM briefing_logs WHERE target_file = ? ORDER BY timestamp DESC",
                (file_path,),
            )
            return Ok([_row_to_briefing_log(row) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get briefing logs for file: {exc}",
                )
            )

    def get_top_directories(
        self, limit: int = 10
    ) -> Result[list[dict[str, Any]], GroundTruthError]:
        """Get directories with most symbols, ranked by symbol count + ref count."""
        try:
            cursor = self.connection.execute("SELECT DISTINCT file_path FROM symbols")
            all_paths = [row["file_path"] for row in cursor.fetchall()]

            dir_stats: dict[str, dict[str, int]] = {}
            for fp in all_paths:
                directory = normalize_path(os.path.dirname(fp)) or "."
                if directory not in dir_stats:
                    dir_stats[directory] = {"symbol_count": 0, "ref_count": 0}
                dir_stats[directory]["symbol_count"] += 1

            # Count refs per directory
            for directory in dir_stats:
                prefix = directory + "/" if directory != "." else ""
                cursor = self.connection.execute(
                    """SELECT COUNT(r.id) as cnt FROM refs r
                       JOIN symbols s ON r.symbol_id = s.id
                       WHERE s.file_path LIKE ? || '%'""",
                    (prefix,),
                )
                row = cursor.fetchone()
                dir_stats[directory]["ref_count"] = row["cnt"] if row else 0

            result_list: list[dict[str, Any]] = [
                {
                    "directory": d,
                    "symbol_count": s["symbol_count"],
                    "ref_count": s["ref_count"],
                }
                for d, s in dir_stats.items()
            ]
            result_list.sort(
                key=lambda x: int(x["symbol_count"]) + int(x["ref_count"]),
                reverse=True,
            )
            return Ok(result_list[:limit])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get top directories: {exc}",
                )
            )

    def get_entry_point_files(self, limit: int = 5) -> Result[list[str], GroundTruthError]:
        """Get files with most incoming references (entry points)."""
        try:
            cursor = self.connection.execute(
                """SELECT s.file_path, COUNT(r.id) as cnt
                   FROM symbols s
                   JOIN refs r ON s.id = r.symbol_id
                   GROUP BY s.file_path
                   ORDER BY COUNT(r.id) DESC
                   LIMIT ?""",
                (limit,),
            )
            return Ok([row["file_path"] for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get entry point files: {exc}",
                )
            )

    def get_symbols_in_line_range(
        self, file_path: str, start_line: int, end_line: int
    ) -> Result[list[SymbolRecord], GroundTruthError]:
        """Get symbols whose range falls within the given line range."""
        try:
            cursor = self.connection.execute(
                """SELECT * FROM symbols
                   WHERE file_path = ? AND line_number >= ? AND end_line <= ?""",
                (file_path, start_line, end_line),
            )
            return Ok([_row_to_symbol(row) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get symbols in line range: {exc}",
                )
            )

    def get_sibling_files(self, file_path: str) -> Result[list[str], GroundTruthError]:
        """Get files in the same directory, excluding the input file."""
        try:
            all_result = self.get_all_files()
            if isinstance(all_result, Err):
                return all_result
            file_path = normalize_path(file_path)
            directory = normalize_path(os.path.dirname(file_path))
            siblings = [
                f
                for f in all_result.value
                if paths_equal(normalize_path(os.path.dirname(f)), directory)
                and not paths_equal(f, file_path)
            ]
            return Ok(sorted(siblings))
        except Exception as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get sibling files: {exc}",
                )
            )

    # --- Index Metadata Operations ---

    def get_file_metadata(self, file_path: str) -> Result[dict[str, Any] | None, GroundTruthError]:
        """Get metadata for a single file."""
        try:
            cursor = self.connection.execute(
                "SELECT * FROM index_metadata WHERE file_path = ?", (file_path,)
            )
            row = cursor.fetchone()
            if row is None:
                return Ok(None)
            return Ok(
                {
                    "file_path": row["file_path"],
                    "mtime": row["mtime"],
                    "size": row["size"],
                    "symbol_count": row["symbol_count"],
                    "indexed_at": row["indexed_at"],
                }
            )
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get file metadata: {exc}",
                )
            )

    def upsert_file_metadata(
        self,
        file_path: str,
        mtime: float,
        size: int,
        symbol_count: int,
        indexed_at: int,
    ) -> Result[None, GroundTruthError]:
        """Insert or update file metadata."""
        try:
            self.connection.execute(
                """INSERT OR REPLACE INTO index_metadata
                   (file_path, mtime, size, symbol_count, indexed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (file_path, mtime, size, symbol_count, indexed_at),
            )
            self.connection.commit()
            return Ok(None)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_insert_failed",
                    message=f"Failed to upsert file metadata: {exc}",
                )
            )

    def get_all_file_metadata(
        self,
    ) -> Result[dict[str, dict[str, Any]], GroundTruthError]:
        """Get metadata for all indexed files, keyed by file_path."""
        try:
            cursor = self.connection.execute("SELECT * FROM index_metadata")
            result: dict[str, dict[str, Any]] = {}
            for row in cursor.fetchall():
                result[row["file_path"]] = {
                    "mtime": row["mtime"],
                    "size": row["size"],
                    "symbol_count": row["symbol_count"],
                    "indexed_at": row["indexed_at"],
                }
            return Ok(result)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get all file metadata: {exc}",
                )
            )

    def delete_file_metadata(self, file_path: str) -> Result[None, GroundTruthError]:
        """Delete metadata for a file."""
        try:
            self.connection.execute("DELETE FROM index_metadata WHERE file_path = ?", (file_path,))
            self.connection.commit()
            return Ok(None)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_delete_failed",
                    message=f"Failed to delete file metadata: {exc}",
                )
            )

    # --- Key-Value Metadata ---

    def get_metadata(self, key: str) -> Result[str | None, GroundTruthError]:
        """Get a metadata value by key."""
        try:
            cursor = self.connection.execute("SELECT value FROM gt_metadata WHERE key = ?", (key,))
            row = cursor.fetchone()
            return Ok(row["value"] if row else None)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get metadata: {exc}",
                )
            )

    def set_metadata(self, key: str, value: str) -> Result[None, GroundTruthError]:
        """Set a metadata key-value pair (upsert)."""
        try:
            self.connection.execute(
                "INSERT INTO gt_metadata (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (key, value, int(time.time())),
            )
            self.connection.commit()
            return Ok(None)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_write_failed",
                    message=f"Failed to set metadata: {exc}",
                )
            )

    # --- Module Coverage Operations ---

    def get_module_symbol_count(self, module_path: str) -> Result[int, GroundTruthError]:
        """Count symbols in files matching a module path."""
        try:
            # Convert dotted module to path prefix
            path_prefix = module_path.replace(".", "/")
            cursor = self.connection.execute(
                """SELECT COUNT(*) as cnt FROM symbols
                   WHERE file_path LIKE ? || '%'""",
                (path_prefix,),
            )
            row = cursor.fetchone()
            count = row["cnt"] if row else 0

            # Also check module_coverage table
            cursor = self.connection.execute(
                "SELECT symbol_count FROM module_coverage WHERE module_path = ?",
                (module_path,),
            )
            row = cursor.fetchone()
            if row and row["symbol_count"] > count:
                count = row["symbol_count"]

            return Ok(count)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get module symbol count: {exc}",
                )
            )

    def module_has_dynamic_exports(self, module_path: str) -> Result[bool, GroundTruthError]:
        """Check if a module has dynamic exports (star imports, __all__, __getattr__)."""
        try:
            cursor = self.connection.execute(
                """SELECT has_star_import, has_dynamic_all, has_dynamic_getattr
                   FROM module_coverage WHERE module_path = ?""",
                (module_path,),
            )
            row = cursor.fetchone()
            if row is None:
                return Ok(False)
            return Ok(
                bool(row["has_star_import"])
                or bool(row["has_dynamic_all"])
                or bool(row["has_dynamic_getattr"])
            )
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to check module dynamic exports: {exc}",
                )
            )

    def upsert_module_coverage(
        self,
        module_path: str,
        symbol_count: int,
        has_star_import: bool,
        has_dynamic_all: bool,
        has_dynamic_getattr: bool,
        indexed_at: int,
    ) -> Result[None, GroundTruthError]:
        """Insert or update module coverage data."""
        try:
            self.connection.execute(
                """INSERT OR REPLACE INTO module_coverage
                   (module_path, symbol_count, has_star_import, has_dynamic_all,
                    has_dynamic_getattr, indexed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    module_path,
                    symbol_count,
                    has_star_import,
                    has_dynamic_all,
                    has_dynamic_getattr,
                    indexed_at,
                ),
            )
            self.connection.commit()
            return Ok(None)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_insert_failed",
                    message=f"Failed to upsert module coverage: {exc}",
                )
            )

    # --- FTS5 Rebuild ---

    def rebuild_fts(self) -> Result[None, GroundTruthError]:
        """Rebuild the FTS5 index from the symbols table."""
        try:
            self.connection.execute("DELETE FROM symbols_fts")
            self.connection.execute(
                """INSERT INTO symbols_fts (rowid, name, file_path, signature, documentation)
                   SELECT id, name, file_path, signature, documentation FROM symbols"""
            )
            self.connection.commit()
            return Ok(None)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_rebuild_failed",
                    message=f"Failed to rebuild FTS index: {exc}",
                )
            )

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
