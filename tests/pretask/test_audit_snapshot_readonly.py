"""Prove the GT_AUDIT_DIR snapshot instrumentation is READ-ONLY.

generate_v1r_brief must return a byte-identical brief whether GT_AUDIT_DIR is set
or unset — the audit block only writes side files, it never touches the brief.
RED if the snapshot block ever mutated a returned value; GREEN by construction.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask.v1r_brief import generate_v1r_brief

_SCHEMA = """
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
    file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
    return_type TEXT, is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
    language TEXT DEFAULT 'python', parent_id INTEGER
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
    source_line INTEGER, source_file TEXT, resolution_method TEXT,
    confidence REAL DEFAULT 0.5, trust_tier TEXT, candidate_count INTEGER,
    evidence_type TEXT, verification_status TEXT, metadata TEXT
);
CREATE TABLE properties (id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT,
    value TEXT, line INTEGER, confidence REAL);
CREATE TABLE assertions (id INTEGER PRIMARY KEY, test_node_id INTEGER,
    target_node_id INTEGER, resolution_score REAL, kind TEXT, expression TEXT, expected TEXT);
CREATE TABLE closure (source INTEGER, target INTEGER, depth INTEGER, min_confidence REAL);
CREATE TABLE file_hashes (file_path TEXT, hash TEXT);
CREATE TABLE project_meta (key TEXT, value TEXT);
CREATE VIRTUAL TABLE nodes_fts USING fts5(name, qualified_name, signature, file_path);
"""


def _build_db(tmp_path: Path) -> tuple[str, str]:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True, exist_ok=True)
    (repo / "pkg" / "config.py").write_text(
        "def parse_config(path):\n    return load_config(path)\n\n"
        "def load_config(path):\n    return {}\n", encoding="utf-8")
    (repo / "pkg" / "app.py").write_text(
        "from pkg.config import parse_config\n\ndef main():\n    return parse_config('c')\n",
        encoding="utf-8")
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,language) VALUES (?,?,?,?,?,?,'python')",
        [(1, "Function", "parse_config", "pkg/config.py", 1, 2),
         (2, "Function", "load_config", "pkg/config.py", 4, 5),
         (3, "Function", "main", "pkg/app.py", 3, 4)])
    con.executemany(
        "INSERT INTO edges (source_id,target_id,type,source_line,source_file,resolution_method,confidence,trust_tier) "
        "VALUES (?,?,'CALLS',?,?,?,?,?)",
        [(3, 1, 4, "pkg/app.py", "import", 1.0, "CERTIFIED"),
         (1, 2, 2, "pkg/config.py", "same_file", 1.0, "CERTIFIED")])
    con.execute("INSERT INTO properties (node_id,kind,value) VALUES (1,'data_flow','path -> load_config(path)')")
    con.executemany("INSERT INTO project_meta VALUES (?,?)",
                    [("schema_version", "v15.2-trust-tier"), ("git_commit", "deadbeef")])
    con.executemany("INSERT INTO nodes_fts (name,qualified_name,signature,file_path) VALUES (?,?,?,?)",
                    [("parse_config", "pkg.config.parse_config", "", "pkg/config.py"),
                     ("load_config", "pkg.config.load_config", "", "pkg/config.py"),
                     ("main", "pkg.app.main", "", "pkg/app.py")])
    con.commit()
    con.close()
    return db, str(repo)


def _brief(issue, repo, db):
    try:
        return generate_v1r_brief(issue_text=issue, repo_root=repo, graph_db=db)
    except TypeError:
        return generate_v1r_brief(issue, repo, db)


def test_audit_snapshot_byte_identical(tmp_path: Path, monkeypatch) -> None:
    db, repo = _build_db(tmp_path)
    issue = "parse_config in pkg/config.py raises when the config file is missing; fix it"

    # --- audit OFF: no GT_AUDIT_DIR ---
    monkeypatch.delenv("GT_AUDIT_DIR", raising=False)
    off_dir = tmp_path / "off"  # must NOT be created/written
    a = _brief(issue, repo, db)

    # --- audit ON: GT_AUDIT_DIR set ---
    adir = tmp_path / "on"
    monkeypatch.setenv("GT_AUDIT_DIR", str(adir))
    b = _brief(issue, repo, db)

    # the brief the agent receives is BYTE-IDENTICAL with/without the audit
    assert a.brief_text == b.brief_text, "audit snapshot changed the brief text"
    assert list(a.sem_components) == list(b.sem_components)
    assert a.rendered_candidate_count == b.rendered_candidate_count
    assert a.effective_w_sem == b.effective_w_sem

    # side effect happens ONLY when on
    assert (adir / "10_candidates_rendered.json").exists(), "snapshot not written when ON"
    assert not off_dir.exists(), "audit wrote outside GT_AUDIT_DIR when OFF"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
