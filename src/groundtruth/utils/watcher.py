"""File change detection for incremental re-indexing."""

from __future__ import annotations

from typing import Callable

from groundtruth.utils.result import GroundTruthError, Result


class FileWatcher:
    """Watches for file changes and triggers re-indexing."""

    def __init__(self, root_path: str, callback: Callable[[list[str]], None]) -> None:
        self._root_path = root_path
        self._callback = callback
        self._running = False

    async def watch(self) -> Result[None, GroundTruthError]:
        """Start watching for file changes. TODO: implement."""
        _ = self  # suppress unused
        raise NotImplementedError("FileWatcher.watch not yet implemented")

    async def stop(self) -> None:
        """Stop watching."""
        self._running = False
