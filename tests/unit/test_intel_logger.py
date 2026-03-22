"""Tests for RepoIntelLogger — append-only summary table logging.

Real scenarios: check-diff results with obligations and contradictions,
multi-file patches, repeated runs accumulating counts, empty results,
schema creation on first write, and cochange pair generation.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from groundtruth.incubator.intel_logger import RepoIntelLogger


@pytest.fixture
def conn() -> sqlite3.Connection:
    """In-memory SQLite for testing."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def logger(conn: sqlite3.Connection) -> RepoIntelLogger:
    return RepoIntelLogger(conn)


class TestSchemaCreation:
    def test_tables_created_on_first_record(self, conn: sqlite3.Connection) -> None:
        """Tables don't exist until first record() call."""
        logger = RepoIntelLogger(conn)
        # Before record — tables should NOT exist
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in tables}
        assert "repo_obligation_stats" not in table_names

        # After record — tables created
        logger.record("check", {"obligations": [], "contradictions": []})
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in tables}
        assert "repo_obligation_stats" in table_names
        assert "repo_convention_stats" in table_names
        assert "repo_confusion_stats" in table_names
        assert "repo_cochange" in table_names

    def test_schema_creation_idempotent(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Calling record() multiple times doesn't error on schema creation."""
        logger.record("check", {"obligations": []})
        logger.record("check", {"obligations": []})
        logger.record("check", {"obligations": []})
        # No crash = success


class TestObligationLogging:
    def test_single_obligation_logged(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """A check-diff result with one obligation → one row in stats."""
        result = {
            "obligations": [
                {"kind": "shared_state", "target": "UserService.save", "confidence": 0.85}
            ],
            "contradictions": [],
        }
        logger.record("check", result)

        rows = conn.execute("SELECT * FROM repo_obligation_stats").fetchall()
        assert len(rows) == 1
        assert rows[0]["subject"] == "UserService.save"
        assert rows[0]["obligation_kind"] == "shared_state"
        assert rows[0]["seen_count"] == 1
        assert abs(rows[0]["confidence_avg"] - 0.85) < 0.01

    def test_repeated_obligation_increments_count(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Same obligation seen 3 times → seen_count=3, confidence averaged."""
        for conf in [0.80, 0.90, 0.85]:
            logger.record("check", {
                "obligations": [
                    {"kind": "caller_contract", "target": "db.query", "confidence": conf}
                ],
                "contradictions": [],
            })

        rows = conn.execute("SELECT * FROM repo_obligation_stats").fetchall()
        assert len(rows) == 1
        assert rows[0]["seen_count"] == 3
        # Running average of 0.80, 0.90, 0.85 should be close to 0.85
        assert abs(rows[0]["confidence_avg"] - 0.85) < 0.05

    def test_different_obligations_different_rows(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Different obligation kinds for same target → separate rows."""
        logger.record("check", {
            "obligations": [
                {"kind": "shared_state", "target": "Cache.get", "confidence": 0.9},
                {"kind": "caller_contract", "target": "Cache.get", "confidence": 0.7},
            ],
            "contradictions": [],
        })

        rows = conn.execute(
            "SELECT * FROM repo_obligation_stats ORDER BY obligation_kind"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["obligation_kind"] == "caller_contract"
        assert rows[1]["obligation_kind"] == "shared_state"

    def test_obligation_without_target_skipped(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Obligation with empty/missing target → not logged."""
        logger.record("check", {
            "obligations": [
                {"kind": "shared_state", "target": "", "confidence": 0.9},
                {"kind": "shared_state", "confidence": 0.9},  # missing target key
            ],
            "contradictions": [],
        })

        rows = conn.execute("SELECT * FROM repo_obligation_stats").fetchall()
        assert len(rows) == 0


class TestContradictionLogging:
    def test_contradiction_logged_as_confusion(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Contradictions map to repo_confusion_stats."""
        logger.record("check", {
            "obligations": [],
            "contradictions": [
                {"kind": "wrong_return_type", "file": "src/auth.py", "message": "..."}
            ],
        })

        rows = conn.execute("SELECT * FROM repo_confusion_stats").fetchall()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "src/auth.py"
        assert rows[0]["confusion_kind"] == "wrong_return_type"
        assert rows[0]["seen_count"] == 1

    def test_repeated_contradiction_increments(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        for _ in range(5):
            logger.record("check", {
                "obligations": [],
                "contradictions": [
                    {"kind": "missing_method", "file": "src/db.py", "message": "x"}
                ],
            })

        rows = conn.execute("SELECT * FROM repo_confusion_stats").fetchall()
        assert len(rows) == 1
        assert rows[0]["seen_count"] == 5


class TestCochangeLogging:
    def test_two_file_cochange(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Two files in obligations → one cochange pair."""
        logger.record("check", {
            "obligations": [
                {"kind": "shared_state", "target": "A.save", "file": "src/a.py", "confidence": 0.9},
                {"kind": "shared_state", "target": "B.load", "file": "src/b.py", "confidence": 0.8},
            ],
            "contradictions": [],
        })

        rows = conn.execute("SELECT * FROM repo_cochange").fetchall()
        assert len(rows) == 1
        assert rows[0]["file_a"] == "src/a.py"
        assert rows[0]["file_b"] == "src/b.py"
        assert rows[0]["seen_count"] == 1

    def test_three_file_cochange_generates_pairs(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Three files → 3 cochange pairs (a-b, a-c, b-c)."""
        logger.record("check", {
            "obligations": [
                {"kind": "x", "target": "A", "file": "a.py", "confidence": 0.9},
                {"kind": "x", "target": "B", "file": "b.py", "confidence": 0.9},
                {"kind": "x", "target": "C", "file": "c.py", "confidence": 0.9},
            ],
            "contradictions": [],
        })

        rows = conn.execute(
            "SELECT * FROM repo_cochange ORDER BY file_a, file_b"
        ).fetchall()
        assert len(rows) == 3
        pairs = [(r["file_a"], r["file_b"]) for r in rows]
        assert ("a.py", "b.py") in pairs
        assert ("a.py", "c.py") in pairs
        assert ("b.py", "c.py") in pairs

    def test_single_file_no_cochange(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Only one file → no cochange pairs."""
        logger.record("check", {
            "obligations": [
                {"kind": "x", "target": "A", "file": "a.py", "confidence": 0.9},
            ],
            "contradictions": [],
        })

        rows = conn.execute("SELECT * FROM repo_cochange").fetchall()
        assert len(rows) == 0

    def test_duplicate_files_deduplicated(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Same file in multiple obligations → deduplicated before pairing."""
        logger.record("check", {
            "obligations": [
                {"kind": "x", "target": "A.a", "file": "src/a.py", "confidence": 0.9},
                {"kind": "y", "target": "A.b", "file": "src/a.py", "confidence": 0.9},
                {"kind": "z", "target": "B.c", "file": "src/b.py", "confidence": 0.9},
            ],
            "contradictions": [],
        })

        rows = conn.execute("SELECT * FROM repo_cochange").fetchall()
        assert len(rows) == 1  # only (a.py, b.py), not (a.py, a.py)

    def test_cochange_accumulates_across_runs(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Same file pair seen in 3 separate check-diff runs → seen_count=3."""
        for _ in range(3):
            logger.record("check", {
                "obligations": [
                    {"kind": "x", "target": "A", "file": "models.py", "confidence": 0.9},
                    {"kind": "x", "target": "B", "file": "views.py", "confidence": 0.9},
                ],
                "contradictions": [],
            })

        rows = conn.execute("SELECT * FROM repo_cochange").fetchall()
        assert len(rows) == 1
        assert rows[0]["seen_count"] == 3


class TestEdgeCases:
    def test_empty_result(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Empty tool result → nothing logged, no crash."""
        logger.record("check", {})
        logger.record("impact", {"obligations": [], "contradictions": []})

    def test_result_without_obligations_key(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Non-check tool (e.g., orient) has no obligations key."""
        logger.record("orient", {"files": 42, "symbols": 100})

    def test_does_not_modify_result(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """Logger must NOT mutate the result dict."""
        result = {
            "obligations": [
                {"kind": "shared_state", "target": "X.y", "confidence": 0.9}
            ],
            "contradictions": [],
        }
        import copy
        original = copy.deepcopy(result)
        logger.record("check", result)
        assert result == original

    def test_large_batch_performance(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """20 obligations + 10 contradictions in one call — must complete quickly."""
        result = {
            "obligations": [
                {"kind": f"kind_{i}", "target": f"Sym{i}.method", "confidence": 0.8}
                for i in range(20)
            ],
            "contradictions": [
                {"kind": f"contra_{i}", "file": f"file_{i}.py", "message": "msg"}
                for i in range(10)
            ],
        }
        start = time.monotonic()
        logger.record("check", result)
        elapsed_ms = (time.monotonic() - start) * 1000
        # Must complete in <100ms (generous for in-memory SQLite)
        assert elapsed_ms < 100, f"Logger took {elapsed_ms:.1f}ms"

        obl_rows = conn.execute("SELECT COUNT(*) as c FROM repo_obligation_stats").fetchone()
        assert obl_rows["c"] == 20
        conf_rows = conn.execute("SELECT COUNT(*) as c FROM repo_confusion_stats").fetchone()
        assert conf_rows["c"] == 10


class TestNoOutputInfluence:
    """The critical contract: logging NEVER changes tool output."""

    def test_logger_does_not_add_keys_to_result(
        self, logger: RepoIntelLogger, conn: sqlite3.Connection
    ) -> None:
        """After logging, result dict has exactly the same keys."""
        result = {"obligations": [], "contradictions": [], "total": 0}
        keys_before = set(result.keys())
        logger.record("check", result)
        keys_after = set(result.keys())
        assert keys_before == keys_after
