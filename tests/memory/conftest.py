"""Test fixtures for memory surface tests."""

from __future__ import annotations

import pytest

# The memory SUBSYSTEM is deliberately excluded from the public repo
# (.gitignore: "src/groundtruth/memory/*", re-including only the embedder files) while
# these TESTS are committed. On a checkout without the subsystem a bare import here
# aborts pytest with exit 4 — which killed the ENTIRE tests/ root of the CI isolate
# sweep (run 30429601995: "sweep aborted: did not run"), turning one absent optional
# package into a false verdict about 900+ unrelated files. importorskip converts that
# into a NAMED per-directory skip: the suite runs wherever the subsystem exists and
# reports honestly where it does not.
pytest.importorskip(
    "groundtruth.memory.config",
    reason="memory subsystem not present in this checkout (deliberately unpublished)",
)

from groundtruth.memory.config import MemoryConfig  # noqa: E402
from groundtruth.memory.db.store import MemoryStore  # noqa: E402
from groundtruth.utils.result import Ok  # noqa: E402


@pytest.fixture
def memory_store() -> MemoryStore:
    """In-memory MemoryStore for tests."""
    store = MemoryStore(":memory:")
    result = store.initialize(config=MemoryConfig(db_path=":memory:"))
    assert isinstance(result, Ok)
    return store


@pytest.fixture
def memory_config() -> MemoryConfig:
    """Default memory config for tests."""
    return MemoryConfig(db_path=":memory:")
