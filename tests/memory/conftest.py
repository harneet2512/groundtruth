"""Test fixtures for memory surface tests."""

from __future__ import annotations

import pytest

from groundtruth.memory.config import MemoryConfig
from groundtruth.memory.db.store import MemoryStore
from groundtruth.utils.result import Ok


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
