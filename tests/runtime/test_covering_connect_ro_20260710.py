"""Graph-F9 (bounce 2026-07-10): covering_runner._connect_ro rw fallback must be
query-only.

The primary open uses ``file:...?mode=ro`` (OS-level read-only). When that open FAILS the
helper falls back to a PLAIN ``sqlite3.connect(db_path)`` — which is READ-WRITE and would
permit a mutation of the authoritative graph.db through a helper named ``_connect_ro``.
Parity with ``curation_map._open_ro`` (which sets ``PRAGMA query_only = 1``): the fallback
must reject writes.
"""
from __future__ import annotations

import sqlite3

import pytest

from groundtruth.runtime import covering_runner as cr


def test_connect_ro_fallback_rejects_writes(tmp_path, monkeypatch):
    db = str(tmp_path / "g.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()

    real_connect = sqlite3.connect

    def fake_connect(*args, **kwargs):
        # Force ONLY the ro-URI attempt (uri=True) to fail so the fallback branch runs;
        # the plain fallback connect delegates to the real driver.
        if kwargs.get("uri"):
            raise sqlite3.OperationalError("forced ro-uri failure")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(cr.sqlite3, "connect", fake_connect)

    conn = cr._connect_ro(db)
    assert conn is not None
    # Reads still work through the fallback handle.
    assert conn.execute("SELECT x FROM t").fetchone()[0] == 1
    # Writes MUST be rejected — the fallback is query-only, never a mutation handle.
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t VALUES (2)")
    conn.close()
