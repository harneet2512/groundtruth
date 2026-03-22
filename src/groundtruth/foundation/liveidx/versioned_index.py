"""Versioned index — two-phase update with query pinning.

Wraps RepresentationStore's version management and ChangeDetector's freshness
tracking to provide atomic index updates with rollback support.
"""

from __future__ import annotations

from groundtruth.foundation.liveidx.change_detector import ChangeDetector
from groundtruth.foundation.repr.store import RepresentationStore
from groundtruth.index.hasher import content_hash


class VersionedIndex:
    """Manages versioned index updates with atomic commit/rollback.

    Two-phase update pattern:
    1. begin_update() - creates new version in 'building' state
    2. Caller processes changed files, stores representations under this version
    3. commit_update() - atomically marks new as 'current', old as 'superseded'
    4. If anything fails between 1 and 3, rollback_update() cleans up
    """

    def __init__(
        self,
        repr_store: RepresentationStore,
        change_detector: ChangeDetector,
    ) -> None:
        self._repr_store = repr_store
        self._change_detector = change_detector

    def begin_update(self) -> int:
        """Start a new index version. Returns version_id.

        Creates a version in 'building' status via the RepresentationStore.
        """
        return self._repr_store.create_version()

    def commit_update(self, version_id: int) -> None:
        """Atomically mark version as 'current', supersede old.

        Single transaction via RepresentationStore.commit_version().
        """
        self._repr_store.commit_version(version_id)

    def rollback_update(self, version_id: int) -> None:
        """Abandon a failed update, clean up.

        Deletes the 'building' version and any representations stored under it.
        """
        self._repr_store.abandon_version(version_id)

    def get_pinned_version(self) -> int | None:
        """Get the current version_id for query pinning.

        Returns the version_id of the 'current' index version, or None
        if no version has been committed yet.
        """
        current = self._repr_store.get_current_version()
        return current.version_id if current is not None else None

    def should_abstain_for_freshness(self, file_path: str) -> bool:
        """If the file is stale, downstream should suppress findings.

        A file is considered stale if its on-disk content hash differs from
        what was stored when it was last indexed.
        """
        stale_files = self._change_detector.get_stale_files()
        return file_path in stale_files
