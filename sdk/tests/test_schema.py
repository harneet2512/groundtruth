from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth._schema import validate_schema
from groundtruth.exceptions import SchemaVersionError


def test_validate_schema_happy(tmp_path: Path) -> None:
    path = tmp_path / "ok.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
          id INTEGER PRIMARY KEY,
          label TEXT NOT NULL,
          name TEXT NOT NULL,
          qualified_name TEXT,
          file_path TEXT NOT NULL,
          start_line INTEGER
        );
        CREATE TABLE edges (
          id INTEGER PRIMARY KEY,
          source_id INTEGER NOT NULL,
          target_id INTEGER NOT NULL,
          type TEXT NOT NULL,
          source_line INTEGER,
          source_file TEXT
        );
        """
    )
    validate_schema(conn)
    conn.close()


def test_validate_schema_missing_table(tmp_path: Path) -> None:
    path = tmp_path / "bad.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE nodes (
          id INTEGER PRIMARY KEY,
          label TEXT NOT NULL,
          name TEXT NOT NULL,
          qualified_name TEXT,
          file_path TEXT NOT NULL,
          start_line INTEGER
        );
        """
    )
    with pytest.raises(SchemaVersionError):
        validate_schema(conn)
    conn.close()


def test_validate_schema_missing_column(tmp_path: Path) -> None:
    path = tmp_path / "badcols.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
          id INTEGER PRIMARY KEY,
          label TEXT NOT NULL,
          name TEXT NOT NULL
        );
        CREATE TABLE edges (
          id INTEGER PRIMARY KEY,
          source_id INTEGER NOT NULL,
          target_id INTEGER NOT NULL,
          type TEXT NOT NULL,
          source_line INTEGER,
          source_file TEXT
        );
        """
    )
    with pytest.raises(SchemaVersionError):
        validate_schema(conn)
    conn.close()
