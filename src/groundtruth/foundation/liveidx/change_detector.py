"""Change detection for incremental indexing.

Tracks file content hashes in SQLite and compares against on-disk state
to detect which files need re-indexing.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from groundtruth.index.hasher import content_hash


TRACKING_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_tracking (
    file_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    last_updated REAL NOT NULL
);
"""


@dataclass
class FileChange:
    """A detected change to a tracked file."""

    file_path: str
    change_type: str  # 'modified' | 'added' | 'deleted'
    old_hash: str | None
    new_hash: str | None


class ChangeDetector:
    """Detects file changes by comparing on-disk content hashes against stored hashes."""

    def __init__(self, db_conn: sqlite3.Connection) -> None:
        self._conn = db_conn
        self._conn.executescript(TRACKING_SCHEMA)

    def detect_changes(self, file_paths: list[str]) -> list[FileChange]:
        """Compare current file state against stored hashes.

        For each file path:
        - If file has no stored hash -> 'added'
        - If file's current hash differs from stored hash -> 'modified'
        - If file cannot be read (deleted) but has stored hash -> 'deleted'
        - If hash matches -> unchanged, not included in results
        """
        changes: list[FileChange] = []
        for path in file_paths:
            stored = self._get_stored_hash(path)
            current = content_hash(path)

            if current is None and stored is not None:
                # File was deleted or unreadable
                changes.append(FileChange(
                    file_path=path,
                    change_type="deleted",
                    old_hash=stored,
                    new_hash=None,
                ))
            elif current is not None and stored is None:
                # New file
                changes.append(FileChange(
                    file_path=path,
                    change_type="added",
                    old_hash=None,
                    new_hash=current,
                ))
            elif current is not None and stored is not None and current != stored:
                # Modified
                changes.append(FileChange(
                    file_path=path,
                    change_type="modified",
                    old_hash=stored,
                    new_hash=current,
                ))
            # else: unchanged or both None — skip

        return changes

    def update_tracking(self, file_path: str, file_hash: str) -> None:
        """Update the stored hash for a file after successful indexing."""
        self._conn.execute(
            """INSERT OR REPLACE INTO file_tracking (file_path, content_hash, last_updated)
               VALUES (?, ?, ?)""",
            (file_path, file_hash, time.time()),
        )
        self._conn.commit()

    def remove_tracking(self, file_path: str) -> None:
        """Remove tracking entry for a deleted file."""
        self._conn.execute(
            "DELETE FROM file_tracking WHERE file_path = ?",
            (file_path,),
        )
        self._conn.commit()

    def get_stale_files(self) -> list[str]:
        """Return files whose on-disk hash differs from stored hash."""
        rows = self._conn.execute(
            "SELECT file_path, content_hash FROM file_tracking"
        ).fetchall()

        stale: list[str] = []
        for path, stored_hash in rows:
            current = content_hash(path)
            if current is None or current != stored_hash:
                stale.append(path)
        return stale

    def get_freshness_report(self) -> dict[str, object]:
        """Return {total_files, stale_files, freshness_ratio, last_update}."""
        rows = self._conn.execute(
            "SELECT file_path, content_hash, last_updated FROM file_tracking"
        ).fetchall()

        total = len(rows)
        if total == 0:
            return {
                "total_files": 0,
                "stale_files": 0,
                "freshness_ratio": 1.0,
                "last_update": None,
            }

        stale_count = 0
        last_update: float = 0.0
        for path, stored_hash, updated_at in rows:
            current = content_hash(path)
            if current is None or current != stored_hash:
                stale_count += 1
            if updated_at > last_update:
                last_update = updated_at

        fresh_count = total - stale_count
        return {
            "total_files": total,
            "stale_files": stale_count,
            "freshness_ratio": fresh_count / total if total > 0 else 1.0,
            "last_update": last_update,
        }

    def _get_stored_hash(self, file_path: str) -> str | None:
        """Get the stored content hash for a file, or None if not tracked."""
        row = self._conn.execute(
            "SELECT content_hash FROM file_tracking WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        return row[0] if row else None
