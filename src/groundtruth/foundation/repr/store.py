"""Storage API for the multi-representation substrate.

Provides CRUD operations over the symbol_representations, symbol_similarity_metadata,
and index_versions tables. Works with any SQLite connection that has the
representation schema created.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from groundtruth.foundation.repr.schema import create_representation_schema


@dataclass
class RepresentationRecord:
    """A stored representation."""

    symbol_id: int
    rep_type: str
    rep_version: str
    rep_blob: bytes
    dim: int | None
    source_hash: str
    index_version: int
    created_at: float


@dataclass
class SimilarityMetadata:
    """Metadata for a symbol used in similarity queries."""

    symbol_id: int
    symbol_kind: str
    file_path: str
    module_path: str | None
    class_name: str | None
    language: str
    arity: int | None
    is_test: bool
    inheritance_root: str | None
    local_scope_key: str | None


@dataclass
class IndexVersion:
    """A snapshot version of the index."""

    version_id: int
    created_at: float
    file_count: int | None
    symbol_count: int | None
    representation_count: int | None
    status: str  # 'building' | 'current' | 'superseded'


class RepresentationStore:
    """CRUD operations for the representation substrate."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        create_representation_schema(conn)

    # ---- Representations ----

    def store_representation(
        self,
        symbol_id: int,
        rep_type: str,
        rep_version: str,
        rep_blob: bytes,
        dim: int | None,
        source_hash: str,
        index_version: int,
    ) -> None:
        """Store or update a representation for a symbol."""
        self._conn.execute(
            """INSERT OR REPLACE INTO symbol_representations
               (symbol_id, rep_type, rep_version, rep_blob, dim, source_hash,
                index_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol_id, rep_type, rep_version, rep_blob, dim, source_hash,
             index_version, time.time()),
        )

    def get_representation(
        self, symbol_id: int, rep_type: str
    ) -> RepresentationRecord | None:
        """Get a single representation for a symbol."""
        row = self._conn.execute(
            """SELECT symbol_id, rep_type, rep_version, rep_blob, dim,
                      source_hash, index_version, created_at
               FROM symbol_representations
               WHERE symbol_id = ? AND rep_type = ?""",
            (symbol_id, rep_type),
        ).fetchone()
        if row is None:
            return None
        return RepresentationRecord(*row)

    def get_all_representations(
        self, rep_type: str, rep_version: str | None = None
    ) -> list[tuple[int, bytes]]:
        """Get all (symbol_id, blob) pairs for a representation type."""
        if rep_version:
            rows = self._conn.execute(
                """SELECT symbol_id, rep_blob FROM symbol_representations
                   WHERE rep_type = ? AND rep_version = ?""",
                (rep_type, rep_version),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT symbol_id, rep_blob FROM symbol_representations
                   WHERE rep_type = ?""",
                (rep_type,),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def delete_stale(self, symbol_id: int, current_source_hash: str) -> int:
        """Delete representations for a symbol whose source hash has changed.

        Returns the number of deleted rows.
        """
        cursor = self._conn.execute(
            """DELETE FROM symbol_representations
               WHERE symbol_id = ? AND source_hash != ?""",
            (symbol_id, current_source_hash),
        )
        return cursor.rowcount

    def delete_symbol_representations(self, symbol_id: int) -> int:
        """Delete all representations for a symbol. Returns deleted count."""
        cursor = self._conn.execute(
            "DELETE FROM symbol_representations WHERE symbol_id = ?",
            (symbol_id,),
        )
        return cursor.rowcount

    # ---- Similarity Metadata ----

    def store_metadata(
        self,
        symbol_id: int,
        symbol_kind: str,
        file_path: str,
        language: str,
        module_path: str | None = None,
        class_name: str | None = None,
        arity: int | None = None,
        is_test: bool = False,
        inheritance_root: str | None = None,
        local_scope_key: str | None = None,
    ) -> None:
        """Store or update similarity metadata for a symbol."""
        self._conn.execute(
            """INSERT OR REPLACE INTO symbol_similarity_metadata
               (symbol_id, symbol_kind, file_path, module_path, class_name,
                language, arity, is_test, inheritance_root, local_scope_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol_id, symbol_kind, file_path, module_path, class_name,
             language, arity, int(is_test), inheritance_root, local_scope_key),
        )

    def get_metadata(self, symbol_id: int) -> SimilarityMetadata | None:
        """Get similarity metadata for a symbol."""
        row = self._conn.execute(
            """SELECT symbol_id, symbol_kind, file_path, module_path, class_name,
                      language, arity, is_test, inheritance_root, local_scope_key
               FROM symbol_similarity_metadata
               WHERE symbol_id = ?""",
            (symbol_id,),
        ).fetchone()
        if row is None:
            return None
        return SimilarityMetadata(
            symbol_id=row[0],
            symbol_kind=row[1],
            file_path=row[2],
            module_path=row[3],
            class_name=row[4],
            language=row[5],
            arity=row[6],
            is_test=bool(row[7]),
            inheritance_root=row[8],
            local_scope_key=row[9],
        )

    def get_metadata_by_scope(
        self,
        scope: str,
        scope_value: str,
    ) -> list[SimilarityMetadata]:
        """Get all metadata matching a scope filter.

        Scope can be: 'same_class', 'same_module', 'same_package', 'language'.
        """
        column_map = {
            "same_class": "class_name",
            "same_module": "file_path",
            "same_package": "module_path",
            "language": "language",
        }
        column = column_map.get(scope)
        if column is None:
            return []

        rows = self._conn.execute(
            f"""SELECT symbol_id, symbol_kind, file_path, module_path, class_name,
                       language, arity, is_test, inheritance_root, local_scope_key
                FROM symbol_similarity_metadata
                WHERE {column} = ?""",
            (scope_value,),
        ).fetchall()
        return [
            SimilarityMetadata(
                symbol_id=r[0], symbol_kind=r[1], file_path=r[2],
                module_path=r[3], class_name=r[4], language=r[5],
                arity=r[6], is_test=bool(r[7]), inheritance_root=r[8],
                local_scope_key=r[9],
            )
            for r in rows
        ]

    # ---- Index Versions ----

    def create_version(
        self,
        file_count: int | None = None,
        symbol_count: int | None = None,
        representation_count: int | None = None,
    ) -> int:
        """Create a new index version in 'building' status. Returns version_id."""
        cursor = self._conn.execute(
            """INSERT INTO index_versions
               (created_at, file_count, symbol_count, representation_count, status)
               VALUES (?, ?, ?, ?, 'building')""",
            (time.time(), file_count, symbol_count, representation_count),
        )
        return cursor.lastrowid  # type: ignore[return-value]

    def commit_version(self, version_id: int) -> None:
        """Mark a version as 'current' and supersede any previous current version."""
        self._conn.execute(
            "UPDATE index_versions SET status = 'superseded' WHERE status = 'current'"
        )
        self._conn.execute(
            "UPDATE index_versions SET status = 'current' WHERE version_id = ?",
            (version_id,),
        )

    def get_current_version(self) -> IndexVersion | None:
        """Get the current index version, if any."""
        row = self._conn.execute(
            """SELECT version_id, created_at, file_count, symbol_count,
                      representation_count, status
               FROM index_versions
               WHERE status = 'current'
               ORDER BY version_id DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            return None
        return IndexVersion(*row)

    def get_version(self, version_id: int) -> IndexVersion | None:
        """Get a specific index version."""
        row = self._conn.execute(
            """SELECT version_id, created_at, file_count, symbol_count,
                      representation_count, status
               FROM index_versions
               WHERE version_id = ?""",
            (version_id,),
        ).fetchone()
        if row is None:
            return None
        return IndexVersion(*row)

    def abandon_version(self, version_id: int) -> None:
        """Delete a 'building' version (cleanup after failure)."""
        self._conn.execute(
            "DELETE FROM index_versions WHERE version_id = ? AND status = 'building'",
            (version_id,),
        )
        # Also clean up any representations that referenced this version
        self._conn.execute(
            "DELETE FROM symbol_representations WHERE index_version = ?",
            (version_id,),
        )
