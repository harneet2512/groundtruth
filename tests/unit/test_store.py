"""Tests for SymbolStore CRUD operations."""

from __future__ import annotations

from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Ok


class TestStoreInitialization:
    def test_initialize_creates_tables(self, in_memory_store: SymbolStore) -> None:
        """Verify all 5 tables + FTS5 exist after initialization."""
        cursor = in_memory_store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row["name"] for row in cursor.fetchall()}
        assert "symbols" in tables
        assert "exports" in tables
        assert "packages" in tables
        assert "refs" in tables
        assert "interventions" in tables
        assert "symbols_fts" in tables


class TestSymbolOperations:
    def test_insert_and_find_symbol(self, in_memory_store: SymbolStore) -> None:
        """Round-trip insert/find by name."""
        result = in_memory_store.insert_symbol(
            name="getUserById",
            kind="function",
            language="python",
            file_path="src/users.py",
            line_number=10,
            end_line=20,
            is_exported=True,
            signature="(user_id: int) -> User",
            params=None,
            return_type="User",
            documentation="Get a user by ID.",
            last_indexed_at=1000,
        )
        assert isinstance(result, Ok)
        symbol_id = result.value
        assert symbol_id > 0

        find_result = in_memory_store.find_symbol_by_name("getUserById")
        assert isinstance(find_result, Ok)
        symbols = find_result.value
        assert len(symbols) == 1
        s = symbols[0]
        assert s.name == "getUserById"
        assert s.kind == "function"
        assert s.language == "python"
        assert s.file_path == "src/users.py"
        assert s.line_number == 10
        assert s.end_line == 20
        assert s.is_exported is True
        assert s.signature == "(user_id: int) -> User"
        assert s.return_type == "User"
        assert s.documentation == "Get a user by ID."
        assert s.usage_count == 0

    def test_insert_duplicate_symbols(self, in_memory_store: SymbolStore) -> None:
        """Same name in different files should both be stored."""
        in_memory_store.insert_symbol(
            name="helper", kind="function", language="python",
            file_path="src/a.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        in_memory_store.insert_symbol(
            name="helper", kind="function", language="python",
            file_path="src/b.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        result = in_memory_store.find_symbol_by_name("helper")
        assert isinstance(result, Ok)
        assert len(result.value) == 2
        files = {s.file_path for s in result.value}
        assert files == {"src/a.py", "src/b.py"}

    def test_get_symbols_in_file(self, in_memory_store: SymbolStore) -> None:
        """Filter symbols by file_path."""
        in_memory_store.insert_symbol(
            name="foo", kind="function", language="python",
            file_path="src/a.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        in_memory_store.insert_symbol(
            name="bar", kind="function", language="python",
            file_path="src/a.py", line_number=10, end_line=15,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        in_memory_store.insert_symbol(
            name="baz", kind="function", language="python",
            file_path="src/b.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        result = in_memory_store.get_symbols_in_file("src/a.py")
        assert isinstance(result, Ok)
        assert len(result.value) == 2
        names = {s.name for s in result.value}
        assert names == {"foo", "bar"}

    def test_delete_symbols_in_file(self, in_memory_store: SymbolStore) -> None:
        """Delete symbols + verify FTS5 cleaned."""
        in_memory_store.insert_symbol(
            name="toDelete", kind="function", language="python",
            file_path="src/del.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        in_memory_store.insert_symbol(
            name="toKeep", kind="function", language="python",
            file_path="src/keep.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )

        del_result = in_memory_store.delete_symbols_in_file("src/del.py")
        assert isinstance(del_result, Ok)
        assert del_result.value == 1

        # Symbol gone from symbols table
        find_result = in_memory_store.find_symbol_by_name("toDelete")
        assert isinstance(find_result, Ok)
        assert len(find_result.value) == 0

        # Symbol gone from FTS5
        fts_result = in_memory_store.search_symbols_fts("toDelete")
        assert isinstance(fts_result, Ok)
        assert len(fts_result.value) == 0

        # Other symbol still there
        keep_result = in_memory_store.find_symbol_by_name("toKeep")
        assert isinstance(keep_result, Ok)
        assert len(keep_result.value) == 1

    def test_get_all_symbol_names(self, in_memory_store: SymbolStore) -> None:
        """Get distinct symbol names."""
        for name in ("alpha", "beta", "alpha"):
            in_memory_store.insert_symbol(
                name=name, kind="variable", language="python",
                file_path=f"src/{name}.py", line_number=1, end_line=1,
                is_exported=False, signature=None, params=None,
                return_type=None, documentation=None, last_indexed_at=1000,
            )
        result = in_memory_store.get_all_symbol_names()
        assert isinstance(result, Ok)
        assert sorted(result.value) == ["alpha", "beta"]

    def test_update_usage_count(self, in_memory_store: SymbolStore) -> None:
        """Update usage_count for a symbol."""
        insert_result = in_memory_store.insert_symbol(
            name="counted", kind="function", language="python",
            file_path="src/c.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(insert_result, Ok)
        sid = insert_result.value

        in_memory_store.update_usage_count(sid, 42)

        find_result = in_memory_store.find_symbol_by_name("counted")
        assert isinstance(find_result, Ok)
        assert find_result.value[0].usage_count == 42


class TestExportOperations:
    def test_insert_and_get_export(self, in_memory_store: SymbolStore) -> None:
        """Exports round-trip."""
        sym_result = in_memory_store.insert_symbol(
            name="MyClass", kind="class", language="typescript",
            file_path="src/models.ts", line_number=1, end_line=50,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(sym_result, Ok)
        sid = sym_result.value

        export_result = in_memory_store.insert_export(
            symbol_id=sid, module_path="src/models", is_default=True, is_named=False,
        )
        assert isinstance(export_result, Ok)
        assert export_result.value > 0

    def test_get_exports_by_module(self, in_memory_store: SymbolStore) -> None:
        """Join query: exports → symbols by module_path."""
        sym1 = in_memory_store.insert_symbol(
            name="Foo", kind="class", language="typescript",
            file_path="src/foo.ts", line_number=1, end_line=10,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(sym1, Ok)
        sym2 = in_memory_store.insert_symbol(
            name="Bar", kind="class", language="typescript",
            file_path="src/foo.ts", line_number=12, end_line=20,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(sym2, Ok)

        in_memory_store.insert_export(symbol_id=sym1.value, module_path="src/foo")
        in_memory_store.insert_export(symbol_id=sym2.value, module_path="src/foo")

        result = in_memory_store.get_exports_by_module("src/foo")
        assert isinstance(result, Ok)
        assert len(result.value) == 2
        names = {s.name for s in result.value}
        assert names == {"Foo", "Bar"}


class TestRefOperations:
    def test_insert_and_get_ref(self, in_memory_store: SymbolStore) -> None:
        """Refs round-trip."""
        sym_result = in_memory_store.insert_symbol(
            name="doStuff", kind="function", language="python",
            file_path="src/stuff.py", line_number=1, end_line=10,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(sym_result, Ok)
        sid = sym_result.value

        ref_result = in_memory_store.insert_ref(
            symbol_id=sid, referenced_in_file="src/main.py",
            referenced_at_line=25, reference_type="call",
        )
        assert isinstance(ref_result, Ok)

    def test_get_refs_for_symbol(self, in_memory_store: SymbolStore) -> None:
        """Filter refs by symbol_id."""
        sym_result = in_memory_store.insert_symbol(
            name="target", kind="function", language="python",
            file_path="src/target.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(sym_result, Ok)
        sid = sym_result.value

        in_memory_store.insert_ref(sid, "src/a.py", 10, "call")
        in_memory_store.insert_ref(sid, "src/b.py", 20, "import")

        result = in_memory_store.get_refs_for_symbol(sid)
        assert isinstance(result, Ok)
        assert len(result.value) == 2
        files = {r.referenced_in_file for r in result.value}
        assert files == {"src/a.py", "src/b.py"}

    def test_get_imports_for_file(self, in_memory_store: SymbolStore) -> None:
        """Refs originating from a file (import type only)."""
        sym_result = in_memory_store.insert_symbol(
            name="dep", kind="function", language="python",
            file_path="src/dep.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(sym_result, Ok)
        sid = sym_result.value

        in_memory_store.insert_ref(sid, "src/consumer.py", 1, "import")
        in_memory_store.insert_ref(sid, "src/consumer.py", 10, "call")

        result = in_memory_store.get_imports_for_file("src/consumer.py")
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        assert result.value[0].reference_type == "import"

    def test_get_importers_of_file(self, in_memory_store: SymbolStore) -> None:
        """Reverse lookup: who references symbols in this file."""
        sym_result = in_memory_store.insert_symbol(
            name="lib_func", kind="function", language="python",
            file_path="src/lib.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(sym_result, Ok)
        sid = sym_result.value

        in_memory_store.insert_ref(sid, "src/app.py", 5, "import")
        in_memory_store.insert_ref(sid, "src/cli.py", 3, "import")

        result = in_memory_store.get_importers_of_file("src/lib.py")
        assert isinstance(result, Ok)
        assert sorted(result.value) == ["src/app.py", "src/cli.py"]


class TestGetSymbolById:
    def test_get_existing_symbol(self, in_memory_store: SymbolStore) -> None:
        """Get a symbol by its primary key."""
        insert_result = in_memory_store.insert_symbol(
            name="myFunc", kind="function", language="python",
            file_path="src/f.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(insert_result, Ok)
        sid = insert_result.value

        result = in_memory_store.get_symbol_by_id(sid)
        assert isinstance(result, Ok)
        assert result.value is not None
        assert result.value.name == "myFunc"
        assert result.value.id == sid

    def test_get_nonexistent_symbol(self, in_memory_store: SymbolStore) -> None:
        """Returns None for nonexistent ID."""
        result = in_memory_store.get_symbol_by_id(9999)
        assert isinstance(result, Ok)
        assert result.value is None


class TestGetRefsFromFile:
    def test_all_refs_from_file(self, in_memory_store: SymbolStore) -> None:
        """Get all refs originating from a file."""
        sym_result = in_memory_store.insert_symbol(
            name="dep", kind="function", language="python",
            file_path="src/dep.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(sym_result, Ok)
        sid = sym_result.value

        in_memory_store.insert_ref(sid, "src/consumer.py", 1, "import")
        in_memory_store.insert_ref(sid, "src/consumer.py", 10, "call")

        result = in_memory_store.get_refs_from_file("src/consumer.py")
        assert isinstance(result, Ok)
        assert len(result.value) == 2

    def test_refs_filtered_by_type(self, in_memory_store: SymbolStore) -> None:
        """Filter refs by reference_type."""
        sym_result = in_memory_store.insert_symbol(
            name="dep2", kind="function", language="python",
            file_path="src/dep2.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(sym_result, Ok)
        sid = sym_result.value

        in_memory_store.insert_ref(sid, "src/user.py", 1, "import")
        in_memory_store.insert_ref(sid, "src/user.py", 10, "call")

        result = in_memory_store.get_refs_from_file("src/user.py", reference_type="import")
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        assert result.value[0].reference_type == "import"

    def test_refs_from_empty_file(self, in_memory_store: SymbolStore) -> None:
        """File with no refs returns empty list."""
        result = in_memory_store.get_refs_from_file("src/empty.py")
        assert isinstance(result, Ok)
        assert result.value == []


class TestPackageOperations:
    def test_insert_and_get_package(self, in_memory_store: SymbolStore) -> None:
        """Packages round-trip."""
        result = in_memory_store.insert_package(
            name="requests", version="2.31.0", package_manager="pip",
        )
        assert isinstance(result, Ok)

        get_result = in_memory_store.get_package("requests")
        assert isinstance(get_result, Ok)
        pkg = get_result.value
        assert pkg is not None
        assert pkg.name == "requests"
        assert pkg.version == "2.31.0"
        assert pkg.package_manager == "pip"
        assert pkg.is_dev_dependency is False

    def test_insert_package_unique_constraint(self, in_memory_store: SymbolStore) -> None:
        """INSERT OR IGNORE on duplicate (name, package_manager)."""
        in_memory_store.insert_package("axios", "1.0.0", "npm")
        in_memory_store.insert_package("axios", "2.0.0", "npm")

        get_result = in_memory_store.get_package("axios")
        assert isinstance(get_result, Ok)
        pkg = get_result.value
        assert pkg is not None
        # First insert wins with INSERT OR IGNORE
        assert pkg.version == "1.0.0"

    def test_get_all_packages(self, in_memory_store: SymbolStore) -> None:
        """Get all packages."""
        in_memory_store.insert_package("foo", "1.0", "npm")
        in_memory_store.insert_package("bar", "2.0", "pip")
        result = in_memory_store.get_all_packages()
        assert isinstance(result, Ok)
        assert len(result.value) == 2

    def test_get_package_not_found(self, in_memory_store: SymbolStore) -> None:
        """Returns None for missing package."""
        result = in_memory_store.get_package("nonexistent")
        assert isinstance(result, Ok)
        assert result.value is None


class TestFTS5Search:
    def test_search_symbols_fts(self, in_memory_store: SymbolStore) -> None:
        """Full-text search matching."""
        in_memory_store.insert_symbol(
            name="getUserById", kind="function", language="python",
            file_path="src/users.py", line_number=1, end_line=10,
            is_exported=True, signature="(id: int) -> User", params=None,
            return_type="User", documentation="Fetch user by ID", last_indexed_at=1000,
        )
        in_memory_store.insert_symbol(
            name="deleteUser", kind="function", language="python",
            file_path="src/users.py", line_number=15, end_line=25,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )

        result = in_memory_store.search_symbols_fts("getUserById")
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        assert result.value[0].name == "getUserById"

    def test_search_symbols_fts_partial(self, in_memory_store: SymbolStore) -> None:
        """FTS5 prefix matching with *."""
        in_memory_store.insert_symbol(
            name="handleRequest", kind="function", language="python",
            file_path="src/handler.py", line_number=1, end_line=10,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        in_memory_store.insert_symbol(
            name="handleResponse", kind="function", language="python",
            file_path="src/handler.py", line_number=15, end_line=25,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )

        result = in_memory_store.search_symbols_fts("handle*")
        assert isinstance(result, Ok)
        assert len(result.value) == 2


class TestGetDeadCode:
    def test_get_dead_code_returns_exported_unused(self, in_memory_store: SymbolStore) -> None:
        """Exported symbols with usage_count=0 are dead code."""
        in_memory_store.insert_symbol(
            name="deadFunc", kind="function", language="python",
            file_path="src/dead.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        in_memory_store.insert_symbol(
            name="deadClass", kind="class", language="python",
            file_path="src/dead.py", line_number=10, end_line=20,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        result = in_memory_store.get_dead_code()
        assert isinstance(result, Ok)
        assert len(result.value) == 2
        names = {s.name for s in result.value}
        assert names == {"deadFunc", "deadClass"}

    def test_get_dead_code_excludes_used(self, in_memory_store: SymbolStore) -> None:
        """Symbols with usage_count > 0 are not dead code."""
        r = in_memory_store.insert_symbol(
            name="usedFunc", kind="function", language="python",
            file_path="src/used.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(r, Ok)
        in_memory_store.update_usage_count(r.value, 5)

        in_memory_store.insert_symbol(
            name="deadFunc", kind="function", language="python",
            file_path="src/dead.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )

        result = in_memory_store.get_dead_code()
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        assert result.value[0].name == "deadFunc"

    def test_get_dead_code_excludes_private(self, in_memory_store: SymbolStore) -> None:
        """Non-exported symbols with usage_count=0 are not returned."""
        in_memory_store.insert_symbol(
            name="privateHelper", kind="function", language="python",
            file_path="src/priv.py", line_number=1, end_line=5,
            is_exported=False, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        result = in_memory_store.get_dead_code()
        assert isinstance(result, Ok)
        assert len(result.value) == 0


class TestGetUnusedPackages:
    def test_unused_packages_detected(self, in_memory_store: SymbolStore) -> None:
        """Packages with no matching import refs are unused."""
        in_memory_store.insert_package("axios", "1.6.0", "npm")
        in_memory_store.insert_package("express", "4.0.0", "npm")

        # Create a symbol that matches "express" import
        r = in_memory_store.insert_symbol(
            name="express", kind="variable", language="typescript",
            file_path="node_modules/express/index.ts", line_number=1, end_line=1,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(r, Ok)
        in_memory_store.insert_ref(r.value, "src/index.ts", 1, "import")

        result = in_memory_store.get_unused_packages()
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        assert result.value[0].name == "axios"

    def test_all_packages_used(self, in_memory_store: SymbolStore) -> None:
        """No unused packages when all are imported."""
        in_memory_store.insert_package("flask", "3.0.0", "pip")
        r = in_memory_store.insert_symbol(
            name="flask", kind="variable", language="python",
            file_path="venv/flask/__init__.py", line_number=1, end_line=1,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(r, Ok)
        in_memory_store.insert_ref(r.value, "src/app.py", 1, "import")

        result = in_memory_store.get_unused_packages()
        assert isinstance(result, Ok)
        assert len(result.value) == 0


class TestGetHotspots:
    def test_hotspots_ordering(self, in_memory_store: SymbolStore) -> None:
        """Symbols ordered by usage_count descending."""
        for name, count in [("low", 2), ("high", 10), ("mid", 5)]:
            r = in_memory_store.insert_symbol(
                name=name, kind="function", language="python",
                file_path=f"src/{name}.py", line_number=1, end_line=5,
                is_exported=True, signature=None, params=None,
                return_type=None, documentation=None, last_indexed_at=1000,
            )
            assert isinstance(r, Ok)
            in_memory_store.update_usage_count(r.value, count)

        result = in_memory_store.get_hotspots()
        assert isinstance(result, Ok)
        assert len(result.value) == 3
        assert result.value[0].name == "high"
        assert result.value[1].name == "mid"
        assert result.value[2].name == "low"

    def test_hotspots_limit(self, in_memory_store: SymbolStore) -> None:
        """Limit parameter caps results."""
        for i in range(5):
            r = in_memory_store.insert_symbol(
                name=f"sym{i}", kind="function", language="python",
                file_path=f"src/s{i}.py", line_number=1, end_line=5,
                is_exported=True, signature=None, params=None,
                return_type=None, documentation=None, last_indexed_at=1000,
            )
            assert isinstance(r, Ok)
            in_memory_store.update_usage_count(r.value, 10 - i)

        result = in_memory_store.get_hotspots(limit=2)
        assert isinstance(result, Ok)
        assert len(result.value) == 2

    def test_hotspots_excludes_zero_usage(self, in_memory_store: SymbolStore) -> None:
        """Symbols with usage_count=0 are not hotspots."""
        in_memory_store.insert_symbol(
            name="unused", kind="function", language="python",
            file_path="src/unused.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        result = in_memory_store.get_hotspots()
        assert isinstance(result, Ok)
        assert len(result.value) == 0


class TestInterventionLogging:
    def test_log_intervention_and_stats(self, in_memory_store: SymbolStore) -> None:
        """Log interventions and verify stats."""
        in_memory_store.log_intervention(
            tool="validate", phase="validate", outcome="fixed_deterministic",
            errors_found=2, errors_fixed=2, ai_called=False, latency_ms=5,
        )
        in_memory_store.log_intervention(
            tool="brief", phase="brief", outcome="valid",
            ai_called=True, ai_model="haiku", tokens_used=150, latency_ms=200,
        )

        result = in_memory_store.get_stats()
        assert isinstance(result, Ok)
        stats = result.value
        assert stats["total_interventions"] == 2
        assert stats["hallucinations_caught"] == 1  # only fixed_deterministic
        assert stats["ai_calls"] == 1
        assert stats["tokens_used"] == 150


class TestBriefingLogOperations:
    def test_insert_and_get_briefing_log(self, in_memory_store: SymbolStore) -> None:
        """Round-trip insert/get briefing log."""
        result = in_memory_store.insert_briefing_log(
            timestamp=1000,
            intent="fix auth flow",
            briefing_text="Use authMiddleware...",
            briefing_symbols=["authMiddleware", "signToken"],
            target_file="src/routes.py",
        )
        assert isinstance(result, Ok)
        log_id = result.value
        assert log_id > 0

        get_result = in_memory_store.get_briefing_log(log_id)
        assert isinstance(get_result, Ok)
        log = get_result.value
        assert log is not None
        assert log.intent == "fix auth flow"
        assert log.briefing_symbols == ["authMiddleware", "signToken"]
        assert log.target_file == "src/routes.py"
        assert log.compliance_rate is None

    def test_get_nonexistent_briefing_log(self, in_memory_store: SymbolStore) -> None:
        """Returns None for nonexistent ID."""
        result = in_memory_store.get_briefing_log(9999)
        assert isinstance(result, Ok)
        assert result.value is None

    def test_link_briefing_to_validation(self, in_memory_store: SymbolStore) -> None:
        """Link a briefing log to a validation intervention."""
        log_result = in_memory_store.insert_briefing_log(
            timestamp=1000, intent="test", briefing_text="text",
            briefing_symbols=["sym"],
        )
        assert isinstance(log_result, Ok)
        log_id = log_result.value

        val_result = in_memory_store.log_intervention(
            tool="validate", phase="validate", outcome="valid",
        )
        assert isinstance(val_result, Ok)
        val_id = val_result.value

        link_result = in_memory_store.link_briefing_to_validation(log_id, val_id)
        assert isinstance(link_result, Ok)

        get_result = in_memory_store.get_briefing_log(log_id)
        assert isinstance(get_result, Ok)
        assert get_result.value is not None
        assert get_result.value.subsequent_validation_id == val_id

    def test_update_briefing_compliance(self, in_memory_store: SymbolStore) -> None:
        """Update compliance data on a briefing log."""
        log_result = in_memory_store.insert_briefing_log(
            timestamp=1000, intent="test", briefing_text="text",
            briefing_symbols=["a", "b", "c"],
        )
        assert isinstance(log_result, Ok)
        log_id = log_result.value

        update_result = in_memory_store.update_briefing_compliance(
            log_id=log_id,
            compliance_rate=0.67,
            symbols_used_correctly=["a", "b"],
            symbols_ignored=["c"],
            hallucinated_despite_briefing=[],
        )
        assert isinstance(update_result, Ok)

        get_result = in_memory_store.get_briefing_log(log_id)
        assert isinstance(get_result, Ok)
        log = get_result.value
        assert log is not None
        assert log.compliance_rate == 0.67
        assert log.symbols_used_correctly == ["a", "b"]
        assert log.symbols_ignored == ["c"]
        assert log.hallucinated_despite_briefing == []

    def test_get_briefing_logs_for_file(self, in_memory_store: SymbolStore) -> None:
        """Filter briefing logs by target file."""
        in_memory_store.insert_briefing_log(
            timestamp=1000, intent="a", briefing_text="t",
            briefing_symbols=["x"], target_file="src/a.py",
        )
        in_memory_store.insert_briefing_log(
            timestamp=1001, intent="b", briefing_text="t",
            briefing_symbols=["y"], target_file="src/b.py",
        )
        in_memory_store.insert_briefing_log(
            timestamp=1002, intent="c", briefing_text="t",
            briefing_symbols=["z"], target_file="src/a.py",
        )

        result = in_memory_store.get_briefing_logs_for_file("src/a.py")
        assert isinstance(result, Ok)
        assert len(result.value) == 2
        # Most recent first
        assert result.value[0].intent == "c"
        assert result.value[1].intent == "a"
