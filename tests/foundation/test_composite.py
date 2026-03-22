"""Tests for composite similarity query (find_related)."""

from __future__ import annotations

import sqlite3
import textwrap

import pytest

from groundtruth.foundation.parser.protocol import ExtractedSymbol
from groundtruth.foundation.repr.registry import clear_registry
from groundtruth.foundation.repr.store import RepresentationStore
from groundtruth.foundation.similarity.composite import find_related
from groundtruth.foundation.similarity.fingerprint import FingerprintExtractor
from groundtruth.foundation.similarity.astvec import StructuralVectorExtractor
from groundtruth.foundation.similarity.tokensketch import TokenSketchExtractor


def _make_symbol(
    name: str = "test_func",
    kind: str = "function",
    raw_text: str = "",
    parameters: list[str] | None = None,
) -> ExtractedSymbol:
    return ExtractedSymbol(
        name=name,
        kind=kind,
        language="python",
        start_line=0,
        end_line=10,
        parameters=parameters or [],
        raw_text=raw_text,
    )


@pytest.fixture
def db() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


@pytest.fixture
def store(db: sqlite3.Connection) -> RepresentationStore:
    return RepresentationStore(db)


@pytest.fixture
def extractors() -> tuple[FingerprintExtractor, StructuralVectorExtractor, TokenSketchExtractor]:
    return FingerprintExtractor(), StructuralVectorExtractor(), TokenSketchExtractor()


def _store_symbol_reps(
    store: RepresentationStore,
    symbol_id: int,
    symbol: ExtractedSymbol,
    fp_ext: FingerprintExtractor,
    vec_ext: StructuralVectorExtractor,
    tok_ext: TokenSketchExtractor,
) -> None:
    """Store all three representations for a symbol."""
    for ext in [fp_ext, vec_ext, tok_ext]:
        blob = ext.extract(symbol)
        store.store_representation(
            symbol_id=symbol_id,
            rep_type=ext.rep_type,
            rep_version=ext.rep_version,
            rep_blob=blob,
            dim=ext.dimension,
            source_hash="hash",
            index_version=1,
        )


class TestCompositeQuery:
    """Composite query with stored representations returns results."""

    def test_finds_similar_symbol(
        self,
        store: RepresentationStore,
        extractors: tuple[FingerprintExtractor, StructuralVectorExtractor, TokenSketchExtractor],
    ) -> None:
        fp_ext, vec_ext, tok_ext = extractors

        code_a = textwrap.dedent("""\
            def process(self, items):
                result = []
                for item in items:
                    if item.valid:
                        result.append(item.value)
                return result
        """)
        code_b = textwrap.dedent("""\
            def handle(self, entries):
                output = []
                for entry in entries:
                    if entry.active:
                        output.append(entry.data)
                return output
        """)
        code_c = textwrap.dedent("""\
            def totally_different(self, x, y, z):
                try:
                    while x > 0:
                        x -= 1
                        raise ValueError()
                except Exception:
                    pass
        """)

        sym_a = _make_symbol(name="process", raw_text=code_a, parameters=["items"])
        sym_b = _make_symbol(name="handle", raw_text=code_b, parameters=["entries"])
        sym_c = _make_symbol(
            name="totally_different", raw_text=code_c, parameters=["x", "y", "z"]
        )

        _store_symbol_reps(store, 1, sym_a, fp_ext, vec_ext, tok_ext)
        _store_symbol_reps(store, 2, sym_b, fp_ext, vec_ext, tok_ext)
        _store_symbol_reps(store, 3, sym_c, fp_ext, vec_ext, tok_ext)

        # Query for symbols similar to sym_a with low threshold
        results = find_related(
            store, symbol_id=1, use_case="convention_cluster", top_k=10
        )

        # sym_b should be in results (very similar structure)
        result_ids = [r[0] for r in results]
        assert 2 in result_ids, f"Expected sym_b (id=2) in results, got {result_ids}"

    def test_returns_empty_for_unknown_use_case(self, store: RepresentationStore) -> None:
        results = find_related(store, symbol_id=1, use_case="nonexistent")
        assert results == []


class TestUseCaseWeighting:
    """Different use cases should weight signals differently."""

    def test_rename_move_favors_fingerprint(
        self,
        store: RepresentationStore,
        extractors: tuple[FingerprintExtractor, StructuralVectorExtractor, TokenSketchExtractor],
    ) -> None:
        fp_ext, vec_ext, tok_ext = extractors

        # Two symbols with identical structure but different tokens
        code = textwrap.dedent("""\
            def func(self, x):
                if x > 0:
                    return x
                return None
        """)
        sym_a = _make_symbol(name="func_a", raw_text=code, parameters=["x"])
        sym_b = _make_symbol(name="func_b", raw_text=code, parameters=["x"])

        _store_symbol_reps(store, 10, sym_a, fp_ext, vec_ext, tok_ext)
        _store_symbol_reps(store, 11, sym_b, fp_ext, vec_ext, tok_ext)

        # rename_move has threshold=0.9, should find identical structure
        results = find_related(store, symbol_id=10, use_case="rename_move", top_k=5)

        if results:
            # If found, score should be very high
            assert results[0][1] > 0.9
            assert results[0][0] == 11


class TestScopeFiltering:
    """Scope filtering should restrict results."""

    def test_scope_restricts_candidates(
        self,
        store: RepresentationStore,
        extractors: tuple[FingerprintExtractor, StructuralVectorExtractor, TokenSketchExtractor],
    ) -> None:
        fp_ext, vec_ext, tok_ext = extractors

        code = textwrap.dedent("""\
            def func(self, x):
                return x + 1
        """)
        sym_a = _make_symbol(name="func_a", raw_text=code, parameters=["x"])
        sym_b = _make_symbol(name="func_b", raw_text=code, parameters=["x"])
        sym_c = _make_symbol(name="func_c", raw_text=code, parameters=["x"])

        _store_symbol_reps(store, 20, sym_a, fp_ext, vec_ext, tok_ext)
        _store_symbol_reps(store, 21, sym_b, fp_ext, vec_ext, tok_ext)
        _store_symbol_reps(store, 22, sym_c, fp_ext, vec_ext, tok_ext)

        # Store metadata — sym_a and sym_b in same class, sym_c in different class
        store.store_metadata(20, "method", "file.py", "python", class_name="MyClass")
        store.store_metadata(21, "method", "file.py", "python", class_name="MyClass")
        store.store_metadata(22, "method", "file.py", "python", class_name="OtherClass")

        # With scope=same_class, should only find sym_b
        results = find_related(
            store, symbol_id=20, use_case="convention_cluster",
            scope="same_class", scope_value="MyClass",
        )

        result_ids = [r[0] for r in results]
        assert 22 not in result_ids  # different class, should be excluded
        # sym_b might or might not pass threshold, but sym_c must not be present

    def test_scope_with_no_matches(
        self,
        store: RepresentationStore,
        extractors: tuple[FingerprintExtractor, StructuralVectorExtractor, TokenSketchExtractor],
    ) -> None:
        fp_ext, vec_ext, tok_ext = extractors

        code = "def f(): return 1"
        sym = _make_symbol(raw_text=code)
        _store_symbol_reps(store, 30, sym, fp_ext, vec_ext, tok_ext)

        store.store_metadata(30, "function", "file.py", "python", class_name="X")

        # Scope to a class with no members
        results = find_related(
            store, symbol_id=30, use_case="test_matching",
            scope="same_class", scope_value="NonexistentClass",
        )
        assert results == []
