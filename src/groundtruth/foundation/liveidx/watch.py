"""Optional file watcher for live indexing.

Uses watchdog for filesystem event monitoring. If watchdog is not installed,
start() raises ImportError with a helpful message.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False


class LiveWatcher:
    """Watches a directory for file changes and notifies a callback.

    Uses watchdog for filesystem event monitoring with debouncing.
    If watchdog is not installed, start() raises ImportError.
    """

    def __init__(
        self,
        root_path: str,
        on_changes: Callable[[list[str]], None],
        debounce_ms: int = 500,
    ) -> None:
        self._root_path = root_path
        self._on_changes = on_changes
        self._debounce_ms = debounce_ms
        self._observer: object | None = None
        self._running = False

    def start(self) -> None:
        """Start watching for file changes.

        Raises ImportError if watchdog is not installed.
        """
        if not HAS_WATCHDOG:
            raise ImportError(
                "watchdog is required for live file watching. "
                "Install it with: pip install watchdog"
            )

        handler = _DebouncedHandler(
            on_changes=self._on_changes,
            debounce_ms=self._debounce_ms,
        )
        observer = Observer()
        observer.schedule(handler, self._root_path, recursive=True)
        observer.start()
        self._observer = observer
        self._running = True

    def stop(self) -> None:
        """Stop watching for file changes."""
        if self._observer is not None and self._running:
            observer = self._observer  # type: ignore[assignment]
            observer.stop()  # type: ignore[union-attr]
            observer.join(timeout=5)  # type: ignore[union-attr]
            self._running = False
            self._observer = None

    @property
    def is_running(self) -> bool:
        """Whether the watcher is currently active."""
        return self._running


if HAS_WATCHDOG:
    class _DebouncedHandler(FileSystemEventHandler):
        """Collects filesystem events and delivers them in batches after a debounce period."""

        def __init__(
            self,
            on_changes: Callable[[list[str]], None],
            debounce_ms: int = 500,
        ) -> None:
            super().__init__()
            self._on_changes = on_changes
            self._debounce_seconds = debounce_ms / 1000.0
            self._pending: set[str] = set()
            self._lock = threading.Lock()
            self._timer: threading.Timer | None = None

        def on_any_event(self, event: FileSystemEvent) -> None:
            """Collect events and schedule debounced delivery."""
            if event.is_directory:
                return
            src_path = str(event.src_path)
            with self._lock:
                self._pending.add(src_path)
                if self._timer is not None:
                    self._timer.cancel()
                self._timer = threading.Timer(
                    self._debounce_seconds, self._flush
                )
                self._timer.start()

        def _flush(self) -> None:
            """Deliver collected changes to the callback."""
            with self._lock:
                if not self._pending:
                    return
                paths = list(self._pending)
                self._pending.clear()
                self._timer = None
            self._on_changes(paths)
