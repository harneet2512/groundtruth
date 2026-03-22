"""Tests for the Foundation v2 representation substrate.

Tests cover:
- Schema creation and coexistence with existing GT schema
- Representation CRUD (store, get, get_all, delete_stale)
- Index version lifecycle (create, commit, supersede, abandon)
- Similarity metadata storage and scoped queries
- Registry registration and lookup
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from groundtruth.foundation.repr.registry import (
    RepresentationExtractor,
    clear_registry,
    get_extractor,
    get_registry,
    register_extractor,
)
from groundtruth.foundation.repr.schema import (
    create_representation_schema,
    has_representation_schema,
)
from groundtruth.foundation.repr.store import RepresentationStore


@pytest.fixture
def db() -> sqlite3.Connection:
    """In-memory SQLite database."""
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def store(db: sqlite3.Connection) -> RepresentationStore:
    """RepresentationStore with schema created."""
    return RepresentationStore(db)


# ---- Schema ----


class TestSchema:
    def test_create_schema(self, db: sqlite3.Connection):
        create_representation_schema(db)
        assert has_representation_schema(db)

    def test_idempotent_creation(self, db: sqlite3.Connection):
        create_representation_schema(db)
        create_representation_schema(db)  # should not raise
        assert has_representation_schema(db)

    def test_schema_empty_initially(self, db: sqlite3.Connection):
        assert not has_representation_schema(db)

    def test_coexists_with_existing_tables(self, db: sqlite3.Connection):
        # Create a fake existing table
        db.execute("CREATE TABLE symbols (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO symbols VALUES (1, 'foo')")

        create_representation_schema(db)

        # Existing table still works
        row = db.execute("SELECT name FROM symbols WHERE id = 1").fetchone()
        assert row[0] == "foo"

        # New tables exist
        assert has_representation_schema(db)


# ---- Representation CRUD ----


class TestRepresentationCRUD:
    def test_store_and_retrieve(self, store: RepresentationStore):
        blob = b"\x01\x02\x03\x04"
        store.store_representation(
            symbol_id=1, rep_type="fingerprint_v1", rep_version="1.0",
            rep_blob=blob, dim=None, source_hash="abc123", index_version=1,
        )

        rec = store.get_representation(1, "fingerprint_v1")
        assert rec is not None
        assert rec.rep_blob == blob
        assert rec.rep_type == "fingerprint_v1"
        assert rec.rep_version == "1.0"
        assert rec.source_hash == "abc123"
        assert rec.index_version == 1
        assert rec.dim is None

    def test_store_with_dimension(self, store: RepresentationStore):
        blob = b"\x00" * 128  # 32 floats
        store.store_representation(
            symbol_id=1, rep_type="astvec_v1", rep_version="1.0",
            rep_blob=blob, dim=32, source_hash="xyz", index_version=1,
        )

        rec = store.get_representation(1, "astvec_v1")
        assert rec is not None
        assert rec.dim == 32

    def test_multiple_rep_types_per_symbol(self, store: RepresentationStore):
        store.store_representation(
            symbol_id=1, rep_type="fingerprint_v1", rep_version="1.0",
            rep_blob=b"\x01", dim=None, source_hash="h1", index_version=1,
        )
        store.store_representation(
            symbol_id=1, rep_type="astvec_v1", rep_version="1.0",
            rep_blob=b"\x02", dim=32, source_hash="h1", index_version=1,
        )
        store.store_representation(
            symbol_id=1, rep_type="tokensketch_v1", rep_version="1.0",
            rep_blob=b"\x03", dim=None, source_hash="h1", index_version=1,
        )

        fp = store.get_representation(1, "fingerprint_v1")
        vec = store.get_representation(1, "astvec_v1")
        sketch = store.get_representation(1, "tokensketch_v1")

        assert fp is not None and fp.rep_blob == b"\x01"
        assert vec is not None and vec.rep_blob == b"\x02"
        assert sketch is not None and sketch.rep_blob == b"\x03"

    def test_get_nonexistent(self, store: RepresentationStore):
        assert store.get_representation(999, "fingerprint_v1") is None

    def test_upsert_overwrites(self, store: RepresentationStore):
        store.store_representation(
            symbol_id=1, rep_type="fp", rep_version="1.0",
            rep_blob=b"old", dim=None, source_hash="h1", index_version=1,
        )
        store.store_representation(
            symbol_id=1, rep_type="fp", rep_version="1.0",
            rep_blob=b"new", dim=None, source_hash="h2", index_version=2,
        )

        rec = store.get_representation(1, "fp")
        assert rec is not None
        assert rec.rep_blob == b"new"
        assert rec.source_hash == "h2"

    def test_get_all_representations(self, store: RepresentationStore):
        for i in range(5):
            store.store_representation(
                symbol_id=i, rep_type="fp", rep_version="1.0",
                rep_blob=bytes([i]), dim=None, source_hash=f"h{i}", index_version=1,
            )

        results = store.get_all_representations("fp")
        assert len(results) == 5
        ids = {r[0] for r in results}
        assert ids == {0, 1, 2, 3, 4}

    def test_get_all_with_version_filter(self, store: RepresentationStore):
        store.store_representation(
            symbol_id=1, rep_type="fp", rep_version="1.0",
            rep_blob=b"\x01", dim=None, source_hash="h1", index_version=1,
        )
        store.store_representation(
            symbol_id=2, rep_type="fp", rep_version="2.0",
            rep_blob=b"\x02", dim=None, source_hash="h2", index_version=1,
        )

        v1 = store.get_all_representations("fp", rep_version="1.0")
        assert len(v1) == 1
        assert v1[0][0] == 1

    def test_delete_stale(self, store: RepresentationStore):
        store.store_representation(
            symbol_id=1, rep_type="fp", rep_version="1.0",
            rep_blob=b"\x01", dim=None, source_hash="old_hash", index_version=1,
        )
        store.store_representation(
            symbol_id=1, rep_type="vec", rep_version="1.0",
            rep_blob=b"\x02", dim=32, source_hash="old_hash", index_version=1,
        )

        deleted = store.delete_stale(1, "new_hash")
        assert deleted == 2

        assert store.get_representation(1, "fp") is None
        assert store.get_representation(1, "vec") is None

    def test_delete_stale_keeps_current(self, store: RepresentationStore):
        store.store_representation(
            symbol_id=1, rep_type="fp", rep_version="1.0",
            rep_blob=b"\x01", dim=None, source_hash="current", index_version=1,
        )

        deleted = store.delete_stale(1, "current")
        assert deleted == 0

        assert store.get_representation(1, "fp") is not None

    def test_delete_symbol_representations(self, store: RepresentationStore):
        store.store_representation(
            symbol_id=1, rep_type="fp", rep_version="1.0",
            rep_blob=b"\x01", dim=None, source_hash="h", index_version=1,
        )
        store.store_representation(
            symbol_id=1, rep_type="vec", rep_version="1.0",
            rep_blob=b"\x02", dim=32, source_hash="h", index_version=1,
        )

        deleted = store.delete_symbol_representations(1)
        assert deleted == 2
        assert store.get_representation(1, "fp") is None


# ---- Similarity Metadata ----


class TestSimilarityMetadata:
    def test_store_and_retrieve(self, store: RepresentationStore):
        store.store_metadata(
            symbol_id=1, symbol_kind="method", file_path="src/foo.py",
            language="python", class_name="Foo", arity=3,
        )

        meta = store.get_metadata(1)
        assert meta is not None
        assert meta.symbol_kind == "method"
        assert meta.file_path == "src/foo.py"
        assert meta.class_name == "Foo"
        assert meta.language == "python"
        assert meta.arity == 3
        assert meta.is_test is False

    def test_upsert(self, store: RepresentationStore):
        store.store_metadata(
            symbol_id=1, symbol_kind="method", file_path="old.py",
            language="python",
        )
        store.store_metadata(
            symbol_id=1, symbol_kind="function", file_path="new.py",
            language="python",
        )

        meta = store.get_metadata(1)
        assert meta is not None
        assert meta.file_path == "new.py"
        assert meta.symbol_kind == "function"

    def test_get_nonexistent(self, store: RepresentationStore):
        assert store.get_metadata(999) is None

    def test_scope_same_class(self, store: RepresentationStore):
        store.store_metadata(1, "method", "a.py", "python", class_name="Foo")
        store.store_metadata(2, "method", "a.py", "python", class_name="Foo")
        store.store_metadata(3, "method", "a.py", "python", class_name="Bar")

        results = store.get_metadata_by_scope("same_class", "Foo")
        assert len(results) == 2
        assert {r.symbol_id for r in results} == {1, 2}

    def test_scope_same_module(self, store: RepresentationStore):
        store.store_metadata(1, "function", "src/a.py", "python")
        store.store_metadata(2, "function", "src/a.py", "python")
        store.store_metadata(3, "function", "src/b.py", "python")

        results = store.get_metadata_by_scope("same_module", "src/a.py")
        assert len(results) == 2

    def test_scope_invalid(self, store: RepresentationStore):
        results = store.get_metadata_by_scope("invalid_scope", "foo")
        assert results == []

    def test_is_test_flag(self, store: RepresentationStore):
        store.store_metadata(1, "function", "tests/test_foo.py", "python", is_test=True)

        meta = store.get_metadata(1)
        assert meta is not None
        assert meta.is_test is True


# ---- Index Versions ----


class TestIndexVersions:
    def test_create_version(self, store: RepresentationStore):
        vid = store.create_version(file_count=10, symbol_count=50)
        assert vid is not None

        v = store.get_version(vid)
        assert v is not None
        assert v.status == "building"
        assert v.file_count == 10
        assert v.symbol_count == 50

    def test_commit_version(self, store: RepresentationStore):
        vid = store.create_version()
        store.commit_version(vid)

        v = store.get_version(vid)
        assert v is not None
        assert v.status == "current"

    def test_commit_supersedes_previous(self, store: RepresentationStore):
        v1 = store.create_version()
        store.commit_version(v1)

        v2 = store.create_version()
        store.commit_version(v2)

        old = store.get_version(v1)
        new = store.get_version(v2)
        assert old is not None and old.status == "superseded"
        assert new is not None and new.status == "current"

    def test_get_current_version(self, store: RepresentationStore):
        assert store.get_current_version() is None

        vid = store.create_version()
        store.commit_version(vid)

        current = store.get_current_version()
        assert current is not None
        assert current.version_id == vid
        assert current.status == "current"

    def test_abandon_version(self, store: RepresentationStore):
        vid = store.create_version()

        # Store a representation under this version
        store.store_representation(
            symbol_id=1, rep_type="fp", rep_version="1.0",
            rep_blob=b"\x01", dim=None, source_hash="h", index_version=vid,
        )

        store.abandon_version(vid)

        assert store.get_version(vid) is None
        assert store.get_representation(1, "fp") is None

    def test_abandon_only_building(self, store: RepresentationStore):
        vid = store.create_version()
        store.commit_version(vid)  # now 'current'

        store.abandon_version(vid)  # should not delete — not 'building'

        assert store.get_version(vid) is not None

    def test_multiple_versions_lifecycle(self, store: RepresentationStore):
        """Full lifecycle: build → commit → build → commit → supersede."""
        v1 = store.create_version(file_count=5)
        store.commit_version(v1)
        assert store.get_current_version().version_id == v1  # type: ignore

        v2 = store.create_version(file_count=6)
        # v2 is building, v1 is still current
        assert store.get_current_version().version_id == v1  # type: ignore

        store.commit_version(v2)
        assert store.get_current_version().version_id == v2  # type: ignore
        assert store.get_version(v1).status == "superseded"  # type: ignore


# ---- Registry ----


class TestRegistry:
    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        clear_registry()
        yield
        clear_registry()

    def test_register_and_retrieve(self):
        class DummyExtractor:
            @property
            def rep_type(self) -> str:
                return "dummy_v1"

            @property
            def rep_version(self) -> str:
                return "1.0"

            @property
            def dimension(self) -> int | None:
                return None

            @property
            def supported_languages(self) -> list[str]:
                return ["python"]

            def extract(self, symbol):
                return b"\x00"

            def distance(self, a, b):
                return 0.0

            def invalidation_key(self, file_path, content):
                return hashlib.sha256(content.encode()).hexdigest()

        ext = DummyExtractor()
        register_extractor(ext)

        assert "dummy_v1" in get_registry()
        assert get_extractor("dummy_v1") is ext

    def test_get_nonexistent_extractor(self):
        assert get_extractor("nonexistent") is None

    def test_clear_registry(self):
        class Ext:
            rep_type = "x"
            rep_version = "1"
            dimension = None
            supported_languages = ["python"]
            def extract(self, s): return b""
            def distance(self, a, b): return 0.0
            def invalidation_key(self, f, c): return ""

        register_extractor(Ext())  # type: ignore
        assert len(get_registry()) == 1
        clear_registry()
        assert len(get_registry()) == 0
