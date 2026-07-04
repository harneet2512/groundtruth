"""TTD tests for post_edit.py TASK #49 / #50 / #47.

Artifact-first: these tests reproduce the OBSERVED defects from the real beets
trajectory using a minimal in-memory/temp graph.db fixture (NOT derived from
reading the implementation).

Beets reference case (frozen artifact):
  - ``importer.py::set_fields`` CALLS ``db.py:722 set_parse(self, key, string: str)``
    -> the decisive callee was listed (or omitted) WITHOUT its contract, so the
       agent could not see what arguments/types set_parse expects. (TASK #49)
  - a SECOND identical ``set_fields`` exists in ``SingletonImportTask`` in the
    SAME file -> the genuine twin was missed; the agent fixed it in a second
    pass. The fuzzy ``[SIMILAR]`` fingerprint match surfaced an UNRELATED
    ``embed_album`` instead. (TASK #50)
  - ``zero.py`` also defines an unrelated ``set_fields(self, item, tags)``
    homonym in a DIFFERENT file -> must NOT be treated as a twin. (TASK #50)
  - non-edge fuzzy signals ([SIMILAR] on weak 2-shared-call matches) injected
    noise unkeyed to the edit. (TASK #47)

Red-before-green: each test below was run against the UNFIXED code and observed
to FAIL, then against the FIXED code and observed to PASS. See the structured
report for captured red/green output.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from groundtruth.hooks import post_edit as pe


# --------------------------------------------------------------------------- #
# Fixture builder — mirrors the graph.db schema (CLAUDE.md) plus the
# `properties` table used by the fingerprint [SIMILAR] path.
# --------------------------------------------------------------------------- #
def _build_beets_graph(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL, parent_id INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL DEFAULT 0.0, metadata TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
            line INTEGER, confidence REAL DEFAULT 1.0
        )"""
    )

    # --- classes (parents) ---
    # id=10 ImportTask, id=11 SingletonImportTask — both in beets/importer.py
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, language, parent_id) "
        "VALUES (10, 'Class', 'ImportTask', 'beets/importer.py', 500, 'python', NULL)"
    )
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, language, parent_id) "
        "VALUES (11, 'Class', 'SingletonImportTask', 'beets/importer.py', 800, 'python', NULL)"
    )

    # --- the edited function: ImportTask.set_fields (id=1) ---
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, signature, "
        "is_exported, is_test, language, parent_id) VALUES "
        "(1, 'Method', 'set_fields', 'beets/importer.py', 602, "
        "'def set_fields(self, lib):', 1, 0, 'python', 10)"
    )

    # --- the TWIN: SingletonImportTask.set_fields, SAME file, different line (id=2) ---
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, signature, "
        "is_exported, is_test, language, parent_id) VALUES "
        "(2, 'Method', 'set_fields', 'beets/importer.py', 845, "
        "'def set_fields(self, lib):', 1, 0, 'python', 11)"
    )

    # --- HOMONYM in a DIFFERENT file: zero.py set_fields(self, item, tags) (id=3) ---
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, signature, "
        "is_exported, is_test, language, parent_id) VALUES "
        "(3, 'Method', 'set_fields', 'beetsplug/zero.py', 88, "
        "'def set_fields(self, item, tags):', 1, 0, 'python', NULL)"
    )

    # --- the decisive callee: db.py set_parse(self, key, string: str) (id=4) ---
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, signature, "
        "is_exported, is_test, language, parent_id) VALUES "
        "(4, 'Method', 'set_parse', 'beets/dbcore/db.py', 722, "
        "'set_parse(self, key, string: str)', 1, 0, 'python', NULL)"
    )

    # --- an UNRELATED fingerprint-similar function: embed_album (id=5) ---
    # Same package dir as the edited fn (beets/) so the fingerprint [SIMILAR]
    # query CAN reach it — this is what made the noisy match render in the real
    # trajectory. Its name shares no tokens with set_fields and no issue overlap.
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, signature, "
        "is_exported, is_test, language, parent_id) VALUES "
        "(5, 'Method', 'embed_album', 'beets/embedart.py', 120, "
        "'def embed_album(self, album):', 1, 0, 'python', NULL)"
    )

    # edge: set_fields (1) CALLS set_parse (4) — verified-strength confidence
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, confidence, resolution_method) "
        "VALUES (1, 4, 'CALLS', 1.0, 'import')"
    )

    conn.commit()
    conn.close()


def _add_weak_similar_fingerprints(db_path: str) -> None:
    """Give the edited fn and embed_album a WEAK (2 shared call) fingerprint match.

    Reproduces the [SIMILAR] noise: embed_album shares only 2 calls with the
    edited function and has no issue/token overlap -> must be suppressed.

    Also adds a real caller of the edited fn so the function is NOT
    G7-isolated; otherwise the isolation gate would strip [SIMILAR] for an
    unrelated reason and the test could not prove the relevance gate is what
    suppresses embed_album.
    """
    conn = sqlite3.connect(db_path)
    # edited fn (id=1): calls foo,bar,set_parse  complexity 5
    conn.execute(
        "INSERT INTO properties (node_id, kind, value) VALUES "
        "(1, 'fingerprint', 'complexity:5|calls:foo,bar,set_parse')"
    )
    # embed_album (id=5): shares foo,bar (2 calls) — weak match, complexity 5
    conn.execute(
        "INSERT INTO properties (node_id, kind, value) VALUES "
        "(5, 'fingerprint', 'complexity:5|calls:foo,bar,resize')"
    )
    # A real caller of set_fields (id=20) so the edited fn is NOT G7-isolated.
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, signature, "
        "is_exported, is_test, language, parent_id) VALUES "
        "(20, 'Function', 'run_import', 'beets/ui/commands.py', 300, "
        "'def run_import(self):', 1, 0, 'python', NULL)"
    )
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, confidence, resolution_method) "
        "VALUES (20, 1, 'CALLS', 1.0, 'import')"
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def beets_db(tmp_path):
    db_path = str(tmp_path / "graph.db")
    _build_beets_graph(db_path)
    return db_path


# --------------------------------------------------------------------------- #
# TASK #49 — callee signatures in "Calls into:"
# --------------------------------------------------------------------------- #
class TestTask49CalleeSignatures:
    def test_pure_helper_renders_signature(self):
        """Unit-level: _format_callee_entry must include the signature."""
        out = pe._format_callee_entry(
            "set_parse", "set_parse(self, key, string: str)", "beets/dbcore/db.py"
        )
        assert out == "set_parse(self, key, string: str) (beets/dbcore/db.py)"

    def test_pure_helper_correct_or_quiet_on_missing_sig(self):
        """Missing signature -> fall back to bare name, never a placeholder."""
        out = pe._format_callee_entry("set_parse", "", "beets/dbcore/db.py")
        assert out == "set_parse (beets/dbcore/db.py)"

    def test_calls_into_render_includes_callee_signature(self, beets_db):
        """End-to-end: the rendered "Calls into:" line must carry the contract.

        RED (unfixed): "Calls into: set_parse (beets/dbcore/db.py)" — no params.
        GREEN (fixed): "Calls into: set_parse(self, key, string: str) (...)".
        """
        out = pe.generate_improved_evidence(
            file_path="beets/importer.py",
            function_names=["set_fields"],
            db_path=beets_db,
            repo_root=str(os.path.dirname(beets_db)),
        )
        assert "Calls into:" in out, f"no callee line rendered:\n{out}"
        assert "set_parse(self, key, string: str)" in out, (
            f"callee signature missing from Calls-into line:\n{out}"
        )


# --------------------------------------------------------------------------- #
# TASK #50 — [TWIN] same-name same-file/class detection
# --------------------------------------------------------------------------- #
class TestTask50Twin:
    def test_twin_query_finds_same_file_sibling(self, beets_db):
        twins = pe._find_same_name_twins(
            beets_db, node_id=1, func_name="set_fields",
            file_path="beets/importer.py",
        )
        lines = {ln for _, ln in twins}
        assert 845 in lines, f"same-file twin at 845 not found: {twins}"

    def test_twin_query_excludes_homonym_in_other_file(self, beets_db):
        """zero.py set_fields is a coincidental homonym, NOT a twin."""
        twins = pe._find_same_name_twins(
            beets_db, node_id=1, func_name="set_fields",
            file_path="beets/importer.py",
        )
        # zero.py is line 88 in a different file — must not appear.
        for _, ln in twins:
            assert ln != 88, f"homonym from zero.py leaked in as twin: {twins}"

    def test_twin_line_present_in_evidence_naming_second_site(self, beets_db):
        """End-to-end: a [TWIN] line must name the second definition site.

        RED (unfixed): no [TWIN] marker exists at all.
        GREEN (fixed): "[TWIN] set_fields() also defined at importer.py:845 ...".
        """
        out = pe.generate_improved_evidence(
            file_path="beets/importer.py",
            function_names=["set_fields"],
            db_path=beets_db,
            repo_root=str(os.path.dirname(beets_db)),
        )
        assert "[TWIN]" in out, f"no [TWIN] marker rendered:\n{out}"
        assert "845" in out, f"[TWIN] did not name the second site (845):\n{out}"


# --------------------------------------------------------------------------- #
# TASK #47 — relevance-gate the [SIMILAR] non-edge noise signal
# --------------------------------------------------------------------------- #
class TestTask47SimilarRelevanceGate:
    def test_relevance_gate_pure_helper(self):
        """No overlap with issue terms OR fn tokens -> suppressed."""
        # embed_album vs edited fn set_fields, unrelated issue
        assert not pe._passes_relevance_gate(
            "embed_album embedart.py",
            issue_terms={"parse", "field", "format"},
            fn_tokens=pe._identifier_tokens("set_fields"),
        )
        # but a same-token candidate passes
        assert pe._passes_relevance_gate(
            "set_value config.py",
            issue_terms=set(),
            fn_tokens=pe._identifier_tokens("set_fields"),
        )

    def test_relevance_gate_correct_or_quiet_no_anchor(self):
        """No issue terms and no fn tokens -> stay silent (drop), not guess."""
        assert not pe._passes_relevance_gate("anything at all", set(), set())

    def test_weak_unrelated_similar_is_suppressed(self, beets_db):
        """End-to-end: a weak (2-shared-call) unrelated [SIMILAR] must NOT render.

        RED (unfixed): "[SIMILAR] embed_album() in embedart.py shares 2 calls".
        GREEN (fixed): no [SIMILAR] line for embed_album (suppressed by gate +
        raised shared-call threshold).
        """
        _add_weak_similar_fingerprints(beets_db)
        out = pe.generate_improved_evidence(
            file_path="beets/importer.py",
            function_names=["set_fields"],
            db_path=beets_db,
            repo_root=str(os.path.dirname(beets_db)),
        )
        assert "embed_album" not in out, (
            f"weak unrelated [SIMILAR] embed_album leaked into evidence:\n{out}"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
