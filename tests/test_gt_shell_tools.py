import json
import sqlite3

from scripts.swebench import gt_shell_tools


def _setup_db(tmp_path):
    db_path = tmp_path / "graph.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            name TEXT,
            qualified_name TEXT,
            file_path TEXT,
            start_line INTEGER,
            signature TEXT,
            return_type TEXT,
            label TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            source_id INTEGER,
            target_id INTEGER,
            type TEXT,
            confidence REAL,
            source_line INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE properties (
            node_id INTEGER,
            kind TEXT,
            value TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return db_path


def test_resolve_symbol_rows_prefers_file_scoped_match(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path)
    monkeypatch.setattr(gt_shell_tools, "DB_PATH", str(db_path))

    conn = gt_shell_tools._conn()
    conn.execute(
        "INSERT INTO nodes (id, name, qualified_name, file_path, start_line, signature, return_type, label) "
        "VALUES (1, '__init__', 'astropy.config.ConfigurationMeta.__init__', 'astropy/config/configuration.py', 10, 'def __init__(cls, name, bases, dict):', '', 'Method')"
    )
    conn.execute(
        "INSERT INTO nodes (id, name, qualified_name, file_path, start_line, signature, return_type, label) "
        "VALUES (2, '__init__', 'astropy.io.ascii.rst.RST.__init__', 'astropy/io/ascii/rst.py', 5, 'def __init__(self, *args, **kwargs):', '', 'Method')"
    )
    conn.commit()

    rows = gt_shell_tools._resolve_symbol_rows(conn, "__init__", file_hint="astropy/io/ascii/rst.py")
    assert len(rows) == 1
    assert rows[0]["file_path"] == "astropy/io/ascii/rst.py"


def test_impact_suppresses_when_file_hint_has_no_match(tmp_path, monkeypatch, capsys):
    db_path = _setup_db(tmp_path)
    monkeypatch.setattr(gt_shell_tools, "DB_PATH", str(db_path))

    conn = gt_shell_tools._conn()
    conn.execute(
        "INSERT INTO nodes (id, name, qualified_name, file_path, start_line, signature, return_type, label) "
        "VALUES (1, 'write', 'astropy.cosmology.io.tests.base.write', 'astropy/cosmology/io/tests/base.py', 10, 'def write(self, cosmo):', '', 'Function')"
    )
    conn.commit()

    gt_shell_tools.impact("write", "astropy/io/ascii/html.py")
    payload = json.loads(capsys.readouterr().out)
    assert payload["tier"] == "possible"
    assert "suppressed" in payload["note"]
