"""Tests for the 3 new evidence modules: issue_grounding, mismatch, format_contract."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


# ---- issue_grounding ----

class TestIssueGrounding:

    def test_extract_code_anchors_backticks(self):
        from groundtruth.evidence.issue_grounding import extract_code_anchors
        anchors = extract_code_anchors("Fix `set_url` to handle `old_url` parameter")
        assert "set_url" in anchors
        assert "old_url" in anchors

    def test_extract_code_anchors_dotted(self):
        from groundtruth.evidence.issue_grounding import extract_code_anchors
        anchors = extract_code_anchors("When remote.set_url fails for renamed repos")
        assert "remote.set_url" in anchors

    def test_extract_code_anchors_param_assign(self):
        from groundtruth.evidence.issue_grounding import extract_code_anchors
        anchors = extract_code_anchors("Pass old_url= when calling set_url")
        assert "old_url" in anchors

    def test_score_evidence_line_relevant(self):
        from groundtruth.evidence.issue_grounding import score_evidence_line
        anchors = ["set_url", "old_url", "remote"]
        score = score_evidence_line("caller uses set_url(old_url=remote.url)", anchors)
        assert score > 0.5

    def test_score_evidence_line_irrelevant(self):
        from groundtruth.evidence.issue_grounding import score_evidence_line
        anchors = ["set_url", "old_url", "remote"]
        score = score_evidence_line("[BLAST-RADIUS] full_options has 29 callers", anchors)
        assert score == 0.0

    def test_rank_promotes_relevant(self):
        from groundtruth.evidence.issue_grounding import rank_evidence_blocks
        blocks = [
            {"text": "[BLAST-RADIUS] full_options 29 callers", "source": "graph"},
            {"text": "set_url(new_url=template, old_url=remote.url)", "source": "graph"},
        ]
        anchors = ["set_url", "old_url", "remote"]
        ranked = rank_evidence_blocks(blocks, anchors)
        assert "set_url" in ranked[0]["text"]

    def test_empty_anchors_preserves_order(self):
        from groundtruth.evidence.issue_grounding import rank_evidence_blocks
        blocks = [{"text": "a"}, {"text": "b"}]
        ranked = rank_evidence_blocks(blocks, [])
        assert ranked[0]["text"] == "a"


# ---- mismatch ----

class TestMismatch:

    def test_extract_removed_identifiers_basic(self):
        from groundtruth.evidence.mismatch import _extract_removed_identifiers
        diff = "-    remote.set_url(new_url=template, old_url=remote.url)\n+    remote.set_url(new_url=template)"
        removed = _extract_removed_identifiers(diff)
        assert "old_url" in removed

    def test_extract_removed_nothing_when_try_except(self):
        from groundtruth.evidence.mismatch import _extract_removed_identifiers
        diff = (
            "-    remote.set_url(new_url=template, old_url=remote.url)\n"
            "+    try:\n"
            "+        remote.set_url(new_url=template, old_url=remote.url)\n"
            "+    except GitCommandError:\n"
            "+        pass"
        )
        removed = _extract_removed_identifiers(diff)
        assert "old_url" not in removed

    def test_detect_stale_references_missing_db(self):
        from groundtruth.evidence.mismatch import detect_stale_references
        result = detect_stale_references("/nonexistent.db", "f.py", "fn", "- old\n+ new")
        assert result == []

    def test_detect_stale_references_filters_low_confidence_callers(self, tmp_path: Path):
        from groundtruth.evidence.mismatch import detect_stale_references

        db_path = str(tmp_path / "graph.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT,
            qualified_name TEXT, file_path TEXT, start_line INTEGER,
            end_line INTEGER, signature TEXT, return_type TEXT,
            is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
            language TEXT, parent_id INTEGER
        )""")
        conn.execute("""CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 1.0, metadata TEXT
        )""")
        conn.execute(
            "INSERT INTO nodes VALUES (1,'Function','set_url','','src/remote.py',10,20,'def set_url()','',1,0,'python',NULL)"
        )
        conn.execute(
            "INSERT INTO nodes VALUES (2,'Function','caller','','src/api.py',1,5,'def caller()','',0,0,'python',NULL)"
        )
        conn.execute(
            "INSERT INTO edges VALUES (1,2,1,'CALLS',3,'src/api.py','name_match',0.2,NULL)"
        )
        conn.commit()
        conn.close()

        api_file = tmp_path / "src" / "api.py"
        api_file.parent.mkdir(parents=True, exist_ok=True)
        api_file.write_text(
            "def caller():\n"
            "    remote = get_remote()\n"
            "    remote.set_url(new_url=template, old_url=remote.url)\n"
        )
        diff = "-    remote.set_url(new_url=template, old_url=remote.url)\n+    remote.set_url(new_url=template)"

        assert detect_stale_references(
            db_path, "src/remote.py", "set_url", diff, str(tmp_path)
        ) == []


# ---- format_contract ----

class TestFormatContract:

    @pytest.fixture
    def graph_db(self, tmp_path: Path) -> str:
        db_path = str(tmp_path / "graph.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT,
            qualified_name TEXT, file_path TEXT, start_line INTEGER,
            end_line INTEGER, signature TEXT, return_type TEXT,
            is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
            language TEXT, parent_id INTEGER
        )""")
        conn.execute("""CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 1.0, metadata TEXT
        )""")
        conn.execute(
            "INSERT INTO nodes VALUES (1,'Function','get_user','','src/users.py',10,20,'def get_user(uid)','dict',1,0,'python',NULL)"
        )
        conn.execute(
            "INSERT INTO nodes VALUES (2,'Function','handle_request','','src/api.py',5,15,'def handle_request(req)','',0,0,'python',NULL)"
        )
        conn.execute(
            "INSERT INTO edges VALUES (1,2,1,'CALLS',8,'src/api.py','import',1.0,NULL)"
        )
        conn.commit()
        conn.close()
        return db_path

    def test_mine_return_shape_missing_db(self):
        from groundtruth.evidence.format_contract import mine_return_shape
        result = mine_return_shape("/nonexistent.db", "f.py", "fn")
        assert result == []

    def test_mine_return_shape_with_graph(self, graph_db: str, tmp_path: Path):
        from groundtruth.evidence.format_contract import _mine_caller_subscripts
        conn = sqlite3.connect(graph_db)
        api_file = tmp_path / "src" / "api.py"
        api_file.parent.mkdir(parents=True, exist_ok=True)
        api_file.write_text(
            'import users\n'
            'from auth import check\n'
            'def handle_request(req):\n'
            '    check(req)\n'
            '    uid = req.uid\n'
            '    # line 6\n'
            '    # line 7\n'
            '    result = get_user(uid)\n'
            '    name = result["name"]\n'
            '    email = result["email"]\n'
        )
        keys = _mine_caller_subscripts(conn, "src/users.py", "get_user", str(tmp_path))
        conn.close()
        assert "name" in keys or "email" in keys

    def test_mine_return_shape_filters_low_confidence_callers(self, graph_db: str, tmp_path: Path):
        from groundtruth.evidence.format_contract import _mine_caller_subscripts

        conn = sqlite3.connect(graph_db)
        conn.execute("UPDATE edges SET confidence = 0.2")
        conn.commit()
        api_file = tmp_path / "src" / "api.py"
        api_file.parent.mkdir(parents=True, exist_ok=True)
        api_file.write_text(
            'def handle_request(req):\n'
            '    result = get_user(req.uid)\n'
            '    name = result["name"]\n'
        )

        keys = _mine_caller_subscripts(conn, "src/users.py", "get_user", str(tmp_path))
        conn.close()
        assert keys == set()
