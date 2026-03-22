"""Tests for RepoIntelReader — decision-time summary queries.

Real scenarios: reading obligation history after multiple check-diff runs,
convention stability tracking, confusion rates, cochange partners,
and the critical contract that LOGGING alone doesn't surface decisions.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from groundtruth.incubator.intel_logger import RepoIntelLogger
from groundtruth.incubator.intel_reader import RepoIntelReader


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def logger(conn: sqlite3.Connection) -> RepoIntelLogger:
    return RepoIntelLogger(conn)


@pytest.fixture
def reader(conn: sqlite3.Connection, logger: RepoIntelLogger) -> RepoIntelReader:
    # Trigger schema creation via logger
    logger.record("check", {"obligations": [], "contradictions": []})
    return RepoIntelReader(conn)


class TestObligationHistory:
    def test_empty_when_no_data(self, reader: RepoIntelReader) -> None:
        result = reader.get_obligation_history(["NonExistent.method"])
        assert result == []

    def test_returns_logged_obligations(
        self, logger: RepoIntelLogger, reader: RepoIntelReader
    ) -> None:
        """Log 3 obligations, then read them back."""
        logger.record("check", {
            "obligations": [
                {"kind": "shared_state", "target": "Cache.get", "confidence": 0.85},
                {"kind": "caller_contract", "target": "Cache.set", "confidence": 0.9},
            ],
            "contradictions": [],
        })

        history = reader.get_obligation_history(["Cache.get", "Cache.set"])
        assert len(history) == 2
        subjects = {h["subject"] for h in history}
        assert "Cache.get" in subjects
        assert "Cache.set" in subjects

    def test_accumulates_seen_count(
        self, logger: RepoIntelLogger, reader: RepoIntelReader
    ) -> None:
        """Same obligation logged 5 times → seen_count=5."""
        for _ in range(5):
            logger.record("check", {
                "obligations": [
                    {"kind": "shared_state", "target": "DB.query", "confidence": 0.8}
                ],
                "contradictions": [],
            })

        history = reader.get_obligation_history(["DB.query"])
        assert len(history) == 1
        assert history[0]["seen_count"] == 5

    def test_ordered_by_seen_count_desc(
        self, logger: RepoIntelLogger, reader: RepoIntelReader
    ) -> None:
        """Most frequently seen obligations come first."""
        for _ in range(3):
            logger.record("check", {
                "obligations": [{"kind": "x", "target": "A.a", "confidence": 0.9}],
                "contradictions": [],
            })
        logger.record("check", {
            "obligations": [{"kind": "x", "target": "B.b", "confidence": 0.9}],
            "contradictions": [],
        })

        history = reader.get_obligation_history(["A.a", "B.b"])
        assert history[0]["subject"] == "A.a"
        assert history[0]["seen_count"] == 3
        assert history[1]["subject"] == "B.b"
        assert history[1]["seen_count"] == 1

    def test_empty_subjects_returns_empty(self, reader: RepoIntelReader) -> None:
        assert reader.get_obligation_history([]) == []


class TestConfusionRate:
    def test_zero_when_no_data(self, reader: RepoIntelReader) -> None:
        assert reader.get_confusion_rate("NonExistent.py") == 0.0

    def test_returns_total_seen_count(
        self, logger: RepoIntelLogger, reader: RepoIntelReader
    ) -> None:
        for _ in range(3):
            logger.record("check", {
                "obligations": [],
                "contradictions": [
                    {"kind": "wrong_type", "file": "src/auth.py", "message": "..."}
                ],
            })

        rate = reader.get_confusion_rate("src/auth.py")
        assert rate == 3.0


class TestCochangePartners:
    def test_empty_when_no_data(self, reader: RepoIntelReader) -> None:
        assert reader.get_cochange_partners("unknown.py") == []

    def test_returns_partner_files(
        self, logger: RepoIntelLogger, reader: RepoIntelReader
    ) -> None:
        logger.record("check", {
            "obligations": [
                {"kind": "x", "target": "A", "file": "models.py", "confidence": 0.9},
                {"kind": "x", "target": "B", "file": "views.py", "confidence": 0.9},
            ],
            "contradictions": [],
        })

        partners = reader.get_cochange_partners("models.py")
        assert len(partners) == 1
        assert partners[0]["partner"] == "views.py"
        assert partners[0]["seen_count"] == 1

    def test_accumulates_cochange_count(
        self, logger: RepoIntelLogger, reader: RepoIntelReader
    ) -> None:
        for _ in range(4):
            logger.record("check", {
                "obligations": [
                    {"kind": "x", "target": "A", "file": "a.py", "confidence": 0.9},
                    {"kind": "x", "target": "B", "file": "b.py", "confidence": 0.9},
                ],
                "contradictions": [],
            })

        partners = reader.get_cochange_partners("a.py")
        assert len(partners) == 1
        assert partners[0]["seen_count"] == 4


class TestLoggingAloneDoesNotSurfaceDecisions:
    """The critical contract: LOGGING flag alone must NOT cause
    _incubator_obligation_history to appear in enriched output."""

    def test_logging_only_no_history_in_output(self, conn: sqlite3.Connection) -> None:
        import os
        from unittest.mock import MagicMock, patch
        from groundtruth.incubator.runtime import IncubatorRuntime

        # Seed some data
        logger = RepoIntelLogger(conn)
        logger.record("check", {
            "obligations": [
                {"kind": "shared_state", "target": "X.y", "confidence": 0.9}
            ],
            "contradictions": [],
        })

        # Enable LOGGING only (not DECISIONS)
        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
        }, clear=True):
            store = MagicMock()
            store.connection = conn
            runtime = IncubatorRuntime(store, "/fake")
            result = {"obligations": [{"target": "X.y", "kind": "shared_state"}]}
            enriched = runtime.enrich("check", result)

        # Must NOT have history — logging doesn't enrich
        assert "_incubator_obligation_history" not in enriched

    def test_decisions_flag_surfaces_history(self, conn: sqlite3.Connection) -> None:
        import os
        from unittest.mock import MagicMock, patch
        from groundtruth.incubator.runtime import IncubatorRuntime

        logger = RepoIntelLogger(conn)
        logger.record("check", {
            "obligations": [
                {"kind": "shared_state", "target": "X.y", "confidence": 0.9}
            ],
            "contradictions": [],
        })

        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
            "GT_ENABLE_REPO_INTEL_DECISIONS": "1",
        }, clear=True):
            store = MagicMock()
            store.connection = conn
            runtime = IncubatorRuntime(store, "/fake")
            result = {"obligations": [{"target": "X.y", "kind": "shared_state"}]}
            enriched = runtime.enrich("check", result)

        assert "_incubator_obligation_history" in enriched
        assert len(enriched["_incubator_obligation_history"]) > 0
