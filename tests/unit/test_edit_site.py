"""Tests for the canonical edit-site resolver."""

from __future__ import annotations

from unittest.mock import MagicMock

from groundtruth.analysis.edit_site import EditSiteResolver
from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.utils.result import Ok


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NEXT_ID = 0


def _sym(
    name: str,
    kind: str = "function",
    file_path: str = "src/service.py",
    line: int | None = 10,
    is_exported: bool = True,
) -> SymbolRecord:
    global _NEXT_ID
    _NEXT_ID += 1
    return SymbolRecord(
        id=_NEXT_ID,
        name=name,
        kind=kind,
        language="python",
        file_path=file_path,
        line_number=line,
        end_line=(line + 20) if line is not None else None,
        is_exported=is_exported,
        signature=None,
        params=None,
        return_type=None,
        documentation=None,
        usage_count=0,
        last_indexed_at=0,
    )


def _resolver(
    symbols: list[SymbolRecord],
    importers: dict[str, list[str]] | None = None,
) -> EditSiteResolver:
    """Build an EditSiteResolver with mocked store and graph."""
    store = MagicMock(spec=SymbolStore)
    graph = MagicMock(spec=ImportGraph)

    store.find_symbol_by_name.return_value = Ok(symbols)

    _importers = importers or {}
    store.get_importers_of_file.side_effect = lambda fp: Ok(_importers.get(fp, []))

    return EditSiteResolver(store, graph)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestDefinitionWinsOverReexport:
    """Symbol defined in source file, re-exported in __init__.py."""

    def test_source_file_ranked_above_init(self) -> None:
        src = _sym("create_user", file_path="src/users/service.py")
        init = _sym("create_user", kind="variable", file_path="src/users/__init__.py")

        resolver = _resolver([src, init])
        result = resolver.resolve("create_user")
        assert not isinstance(result, type(None))

        candidates = result.value
        assert len(candidates) == 2
        assert candidates[0].file_path == "src/users/service.py"
        assert candidates[0].score > candidates[1].score


class TestSourceBeatsTestFile:
    """Symbol in test file vs source file — source file wins."""

    def test_source_ranked_above_test(self) -> None:
        src = _sym("parse_token", file_path="src/auth/tokens.py")
        test = _sym("parse_token", file_path="tests/test_auth.py")

        resolver = _resolver([src, test])
        candidates = resolver.resolve("parse_token").value

        assert candidates[0].file_path == "src/auth/tokens.py"
        assert candidates[1].file_path == "tests/test_auth.py"
        assert candidates[0].score > candidates[1].score


class TestHighFanInBeatsLowFanIn:
    """File with more importers is ranked higher."""

    def test_fan_in_boosts_score(self) -> None:
        popular = _sym("db_connect", file_path="src/db/client.py")
        obscure = _sym("db_connect", file_path="src/db/legacy.py")

        importers = {
            "src/db/client.py": ["a.py", "b.py", "c.py", "d.py", "e.py"],
            "src/db/legacy.py": [],
        }
        resolver = _resolver([popular, obscure], importers=importers)
        candidates = resolver.resolve("db_connect").value

        assert candidates[0].file_path == "src/db/client.py"
        assert candidates[0].score > candidates[1].score


class TestAmbiguousCase:
    """Two source files both define same symbol with similar scores → ambiguous."""

    def test_close_scores_marked_ambiguous(self) -> None:
        a = _sym("handle_event", file_path="src/events/handler_a.py")
        b = _sym("handle_event", file_path="src/events/handler_b.py")

        # Same importers so scores are identical
        importers = {
            "src/events/handler_a.py": ["x.py"],
            "src/events/handler_b.py": ["y.py"],
        }
        resolver = _resolver([a, b], importers=importers)
        candidates = resolver.resolve("handle_event").value

        assert len(candidates) == 2
        assert candidates[0].is_ambiguous is True
        assert candidates[1].is_ambiguous is True


class TestSymbolNotFound:
    """Symbol not in the store → empty list."""

    def test_empty_result(self) -> None:
        resolver = _resolver([])
        candidates = resolver.resolve("nonexistent").value
        assert candidates == []


class TestPenaltyPaths:
    """Files in docs/migrations/config get penalized."""

    def test_docs_path_penalized(self) -> None:
        src = _sym("Config", kind="class", file_path="src/core/config.py")
        docs = _sym("Config", kind="class", file_path="docs/examples/config.py")

        resolver = _resolver([src, docs])
        candidates = resolver.resolve("Config").value

        assert candidates[0].file_path == "src/core/config.py"
        assert candidates[0].score > candidates[1].score


class TestMaxCandidates:
    """Respects max_candidates limit."""

    def test_limits_output(self) -> None:
        syms = [_sym("foo", file_path=f"src/mod{i}.py") for i in range(10)]
        resolver = _resolver(syms)
        candidates = resolver.resolve("foo", max_candidates=3).value
        assert len(candidates) == 3
