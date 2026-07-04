"""JSONL trace writer.

Writes one JSON line per endpoint call to a local file.
Configurable via environment variable or explicit path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TextIO

from groundtruth.observability.schema import EndpointTrace

# Default: .groundtruth/traces/ relative to repo root
_DEFAULT_DIR = ".groundtruth/traces"
_ENV_VAR = "GT_TRACE_DIR"
_ENV_ENABLED = "GT_TRACE_ENABLED"


def _default_trace_dir() -> Path:
    """Resolve trace directory from env or default."""
    env_dir = os.environ.get(_ENV_VAR)
    if env_dir:
        return Path(env_dir)
    return Path(_DEFAULT_DIR)


def is_tracing_enabled() -> bool:
    """Check if tracing is enabled. Default: enabled."""
    val = os.environ.get(_ENV_ENABLED, "1").lower()
    return val not in ("0", "false", "no", "off")


class TraceWriter:
    """Appends EndpointTrace records as JSONL to a file.

    File naming: {trace_dir}/gt_traces.jsonl
    Append-only. No rotation — kept simple for now.
    """

    def __init__(
        self,
        trace_dir: Path | str | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        self._enabled = enabled if enabled is not None else is_tracing_enabled()
        self._dir = Path(trace_dir) if trace_dir else _default_trace_dir()
        self._file: TextIO | None = None
        self._trace_count = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def trace_file(self) -> Path:
        return self._dir / "gt_traces.jsonl"

    @property
    def trace_count(self) -> int:
        return self._trace_count

    def write(self, trace: EndpointTrace) -> None:
        """Append one trace as a JSON line."""
        if not self._enabled:
            return
        self._ensure_open()
        assert self._file is not None
        line = json.dumps(trace.to_dict(), separators=(",", ":"))
        self._file.write(line + "\n")
        self._file.flush()
        self._trace_count += 1

    def close(self) -> None:
        """Close the file handle."""
        if self._file and not self._file.closed:
            self._file.close()
        self._file = None

    def _ensure_open(self) -> None:
        """Lazily open the trace file."""
        if self._file is not None and not self._file.closed:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = open(self.trace_file, "a", encoding="utf-8")

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
