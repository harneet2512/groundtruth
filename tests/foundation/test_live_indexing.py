"""Tests for Phase 5 — versioned live indexing.

Tests ChangeDetector, VersionedIndex, and LiveWatcher.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time

import pytest

from groundtruth.foundation.liveidx.change_detector import ChangeDetector, FileChange
from groundtruth.foundation.liveidx.versioned_index import VersionedIndex
from groundtruth.foundation.liveidx.watch import LiveWatcher, HAS_WATCHDOG
from groundtruth.foundation.repr.store import RepresentationStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_conn() -> sqlite3.Connection:
    """In-memory SQLite connection."""
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def detector(db_conn: sqlite3.Connection) -> ChangeDetector:
    return ChangeDetector(db_conn)


@pytest.fixture
def repr_store(db_conn: sqlite3.Connection) -> RepresentationStore:
    return RepresentationStore(db_conn)


@pytest.fixture
def versioned_index(
    repr_store: RepresentationStore, detector: ChangeDetector
) -> VersionedIndex:
    return VersionedIndex(repr_store, detector)


@pytest.fixture
def tmp_dir() -> str:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as d:
        yield d


def _write_file(directory: str, name: str, content: str) -> str:
    """Write a file and return its path."""
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write(content)
    return path


# ---------------------------------------------------------------------------
# ChangeDetector tests
# ---------------------------------------------------------------------------

class TestChangeDetector:
    def test_detect_added_file(
        self, detector: ChangeDetector, tmp_dir: str
    ) -> None:
        """A file with no stored hash is detected as 'added'."""
        path = _write_file(tmp_dir, "new.py", "print('hello')")
        changes = detector.detect_changes([path])
        assert len(changes) == 1
        assert changes[0].change_type == "added"
        assert changes[0].old_hash is None
        assert changes[0].new_hash is not None

    def test_detect_modified_file(
        self, detector: ChangeDetector, tmp_dir: str
    ) -> None:
        """A file whose content changed after tracking is detected as 'modified'."""
        path = _write_file(tmp_dir, "mod.py", "v1")
        from groundtruth.index.hasher import content_hash

        h1 = content_hash(path)
        assert h1 is not None
        detector.update_tracking(path, h1)

        # Modify the file
        _write_file(tmp_dir, "mod.py", "v2")
        changes = detector.detect_changes([path])
        assert len(changes) == 1
        assert changes[0].change_type == "modified"
        assert changes[0].old_hash == h1
        assert changes[0].new_hash is not None
        assert changes[0].new_hash != h1

    def test_detect_unchanged_file(
        self, detector: ChangeDetector, tmp_dir: str
    ) -> None:
        """A file with unchanged content is not included in changes."""
        path = _write_file(tmp_dir, "same.py", "unchanged")
        from groundtruth.index.hasher import content_hash

        h = content_hash(path)
        assert h is not None
        detector.update_tracking(path, h)

        changes = detector.detect_changes([path])
        assert len(changes) == 0

    def test_detect_deleted_file(
        self, detector: ChangeDetector, tmp_dir: str
    ) -> None:
        """A tracked file that no longer exists is detected as 'deleted'."""
        path = _write_file(tmp_dir, "gone.py", "bye")
        from groundtruth.index.hasher import content_hash

        h = content_hash(path)
        assert h is not None
        detector.update_tracking(path, h)

        os.remove(path)
        changes = detector.detect_changes([path])
        assert len(changes) == 1
        assert changes[0].change_type == "deleted"
        assert changes[0].old_hash == h
        assert changes[0].new_hash is None

    def test_update_tracking_then_no_change(
        self, detector: ChangeDetector, tmp_dir: str
    ) -> None:
        """After update_tracking, detect_changes shows no change for same content."""
        path = _write_file(tmp_dir, "track.py", "content")
        from groundtruth.index.hasher import content_hash

        h = content_hash(path)
        assert h is not None
        detector.update_tracking(path, h)

        changes = detector.detect_changes([path])
        assert len(changes) == 0

    def test_get_stale_files(
        self, detector: ChangeDetector, tmp_dir: str
    ) -> None:
        """get_stale_files returns files whose content changed since tracking."""
        from groundtruth.index.hasher import content_hash

        p1 = _write_file(tmp_dir, "a.py", "original_a")
        p2 = _write_file(tmp_dir, "b.py", "original_b")

        h1 = content_hash(p1)
        h2 = content_hash(p2)
        assert h1 is not None and h2 is not None
        detector.update_tracking(p1, h1)
        detector.update_tracking(p2, h2)

        # Modify only p1
        _write_file(tmp_dir, "a.py", "changed_a")

        stale = detector.get_stale_files()
        assert p1 in stale
        assert p2 not in stale

    def test_get_freshness_report(
        self, detector: ChangeDetector, tmp_dir: str
    ) -> None:
        """Freshness report counts are correct."""
        from groundtruth.index.hasher import content_hash

        p1 = _write_file(tmp_dir, "x.py", "x_content")
        p2 = _write_file(tmp_dir, "y.py", "y_content")

        h1 = content_hash(p1)
        h2 = content_hash(p2)
        assert h1 is not None and h2 is not None
        detector.update_tracking(p1, h1)
        detector.update_tracking(p2, h2)

        # Modify p1 to make it stale
        _write_file(tmp_dir, "x.py", "x_changed")

        report = detector.get_freshness_report()
        assert report["total_files"] == 2
        assert report["stale_files"] == 1
        assert report["freshness_ratio"] == 0.5
        assert report["last_update"] is not None

    def test_freshness_report_empty(self, detector: ChangeDetector) -> None:
        """Empty tracker returns ratio 1.0."""
        report = detector.get_freshness_report()
        assert report["total_files"] == 0
        assert report["stale_files"] == 0
        assert report["freshness_ratio"] == 1.0
        assert report["last_update"] is None


# ---------------------------------------------------------------------------
# VersionedIndex tests
# ---------------------------------------------------------------------------

class TestVersionedIndex:
    def test_begin_update_creates_building_version(
        self, versioned_index: VersionedIndex, repr_store: RepresentationStore
    ) -> None:
        """begin_update creates a version in 'building' status."""
        vid = versioned_index.begin_update()
        version = repr_store.get_version(vid)
        assert version is not None
        assert version.status == "building"

    def test_commit_update_marks_current(
        self, versioned_index: VersionedIndex, repr_store: RepresentationStore
    ) -> None:
        """commit_update marks version as 'current'."""
        vid = versioned_index.begin_update()
        versioned_index.commit_update(vid)
        version = repr_store.get_version(vid)
        assert version is not None
        assert version.status == "current"

    def test_commit_supersedes_old(
        self, versioned_index: VersionedIndex, repr_store: RepresentationStore
    ) -> None:
        """Committing a new version supersedes the old 'current' version."""
        v1 = versioned_index.begin_update()
        versioned_index.commit_update(v1)

        v2 = versioned_index.begin_update()
        versioned_index.commit_update(v2)

        old = repr_store.get_version(v1)
        new = repr_store.get_version(v2)
        assert old is not None and old.status == "superseded"
        assert new is not None and new.status == "current"

    def test_rollback_cleans_up(
        self, versioned_index: VersionedIndex, repr_store: RepresentationStore
    ) -> None:
        """rollback_update removes the building version."""
        vid = versioned_index.begin_update()
        versioned_index.rollback_update(vid)
        version = repr_store.get_version(vid)
        assert version is None

    def test_get_pinned_version(
        self, versioned_index: VersionedIndex
    ) -> None:
        """get_pinned_version returns the current version_id."""
        assert versioned_index.get_pinned_version() is None

        vid = versioned_index.begin_update()
        versioned_index.commit_update(vid)
        assert versioned_index.get_pinned_version() == vid

    def test_should_abstain_fresh_file(
        self, versioned_index: VersionedIndex, detector: ChangeDetector, tmp_dir: str
    ) -> None:
        """Fresh file -> should_abstain returns False."""
        from groundtruth.index.hasher import content_hash

        path = _write_file(tmp_dir, "fresh.py", "content")
        h = content_hash(path)
        assert h is not None
        detector.update_tracking(path, h)

        assert versioned_index.should_abstain_for_freshness(path) is False

    def test_should_abstain_stale_file(
        self, versioned_index: VersionedIndex, detector: ChangeDetector, tmp_dir: str
    ) -> None:
        """Stale file -> should_abstain returns True."""
        from groundtruth.index.hasher import content_hash

        path = _write_file(tmp_dir, "stale.py", "v1")
        h = content_hash(path)
        assert h is not None
        detector.update_tracking(path, h)

        # Modify to make stale
        _write_file(tmp_dir, "stale.py", "v2")
        assert versioned_index.should_abstain_for_freshness(path) is True

    def test_concurrent_update_old_version_accessible(
        self, versioned_index: VersionedIndex, repr_store: RepresentationStore
    ) -> None:
        """During an update, the old 'current' version remains accessible."""
        v1 = versioned_index.begin_update()
        versioned_index.commit_update(v1)

        # Start v2 but don't commit yet
        v2 = versioned_index.begin_update()

        # Old version should still be pinned
        assert versioned_index.get_pinned_version() == v1
        old = repr_store.get_version(v1)
        assert old is not None and old.status == "current"

        # Now commit v2
        versioned_index.commit_update(v2)
        assert versioned_index.get_pinned_version() == v2


# ---------------------------------------------------------------------------
# LiveWatcher tests
# ---------------------------------------------------------------------------

class TestLiveWatcher:
    def test_no_watchdog_raises_import_error(self, tmp_dir: str) -> None:
        """If watchdog is not installed, start() raises ImportError."""
        import groundtruth.foundation.liveidx.watch as watch_module

        original = watch_module.HAS_WATCHDOG
        try:
            watch_module.HAS_WATCHDOG = False
            watcher = LiveWatcher(tmp_dir, on_changes=lambda paths: None)
            with pytest.raises(ImportError, match="watchdog"):
                watcher.start()
        finally:
            watch_module.HAS_WATCHDOG = original

    @pytest.mark.skipif(not HAS_WATCHDOG, reason="watchdog not installed")
    def test_start_stop_lifecycle(self, tmp_dir: str) -> None:
        """If watchdog is installed, start/stop lifecycle works."""
        watcher = LiveWatcher(tmp_dir, on_changes=lambda paths: None)
        assert not watcher.is_running
        watcher.start()
        assert watcher.is_running
        watcher.stop()
        assert not watcher.is_running
