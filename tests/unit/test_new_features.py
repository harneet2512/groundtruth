"""Tests for new features: readiness probe, tracing, persistent index,
path validation, sanitization, crash recovery, meta-tool, parallel indexing."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from groundtruth.index.indexer import Indexer
from groundtruth.index.store import SymbolStore
from groundtruth.lsp.client import LSPClient
from groundtruth.lsp.manager import LSPManager
from groundtruth.mcp.response import ToolResponse
from groundtruth.mcp.tools import _check_path, _detect_operation, handle_do
from groundtruth.utils.platform import validate_path
from groundtruth.utils.result import Err, GroundTruthError, Ok
from groundtruth.utils.sanitize import sanitize_for_prompt
from tests.conftest import MockStreamReader, make_lsp_message


# ---------------------------------------------------------------------------
# Phase 1.1: LSP Readiness Probe
# ---------------------------------------------------------------------------


class TestProbeReady:
    @pytest.mark.asyncio
    async def test_probe_ready_succeeds_on_response(self) -> None:
        mock_proc = AsyncMock()
        mock_stdout = MockStreamReader()
        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock()
        mock_stdin.drain = AsyncMock()

        mock_proc.stdin = mock_stdin
        mock_proc.stdout = mock_stdout
        mock_proc.returncode = None

        client = LSPClient(["fake"], "file:///tmp")
        client._process = mock_proc
        client._started = True

        # Queue workspace/symbol response
        response = {"jsonrpc": "2.0", "id": 1, "result": []}
        mock_stdout.feed_data(make_lsp_message(response))

        result = await client.probe_ready(timeout=3.0, interval=0.5)
        assert result is True

    @pytest.mark.asyncio
    async def test_probe_ready_succeeds_on_error_response(self) -> None:
        mock_proc = AsyncMock()
        mock_stdout = MockStreamReader()
        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock()
        mock_stdin.drain = AsyncMock()

        mock_proc.stdin = mock_stdin
        mock_proc.stdout = mock_stdout
        mock_proc.returncode = None

        client = LSPClient(["fake"], "file:///tmp")
        client._process = mock_proc
        client._started = True

        # Queue error response (still means server is alive)
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        }
        mock_stdout.feed_data(make_lsp_message(response))

        result = await client.probe_ready(timeout=3.0, interval=0.5)
        assert result is True

    @pytest.mark.asyncio
    async def test_probe_ready_times_out(self) -> None:
        mock_proc = AsyncMock()
        mock_stdout = MockStreamReader()
        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock()
        mock_stdin.drain = AsyncMock()

        mock_proc.stdin = mock_stdin
        mock_proc.stdout = mock_stdout
        mock_proc.returncode = None

        client = LSPClient(["fake"], "file:///tmp")
        client._process = mock_proc
        client._started = True

        # Don't feed any data — should timeout
        mock_stdout.feed_eof()

        result = await client.probe_ready(timeout=0.5, interval=0.2)
        assert result is False


# ---------------------------------------------------------------------------
# Phase 1.2: LSP Trace File
# ---------------------------------------------------------------------------


class TestTraceFile:
    def test_trace_log_writes_jsonl(self, tmp_path: Path) -> None:
        trace_path = tmp_path / "trace.jsonl"
        client = LSPClient(["fake"], "file:///tmp", trace_path=trace_path)

        client._trace_log("send", {"method": "initialize", "id": 1})
        client._trace_log("recv", {"id": 1, "result": {}})

        lines = trace_path.read_text().strip().splitlines()
        assert len(lines) == 2

        entry1 = json.loads(lines[0])
        assert entry1["direction"] == "send"
        assert entry1["message"]["method"] == "initialize"

        entry2 = json.loads(lines[1])
        assert entry2["direction"] == "recv"

    def test_trace_truncation(self, tmp_path: Path) -> None:
        trace_path = tmp_path / "trace.jsonl"
        client = LSPClient(["fake"], "file:///tmp", trace_path=trace_path)

        # Send a message larger than 10KB
        big_msg: dict[str, Any] = {"data": "x" * 20000}
        client._trace_log("send", big_msg)

        lines = trace_path.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert "truncated" in str(entry["message"])

    def test_rotate_traces(self, tmp_path: Path) -> None:
        # Create 4 existing trace files
        for i in range(4):
            f = tmp_path / f"lsp-trace-{i}.jsonl"
            f.write_text(f"line {i}")
            # Ensure different mtimes
            os.utime(f, (time.time() - (4 - i), time.time() - (4 - i)))

        trace_path = tmp_path / "lsp-trace-new.jsonl"
        client = LSPClient(["fake"], "file:///tmp", trace_path=trace_path)
        client._rotate_traces()

        remaining = list(tmp_path.glob("lsp-trace-*.jsonl"))
        assert len(remaining) <= 3

    def test_no_trace_when_disabled(self) -> None:
        client = LSPClient(["fake"], "file:///tmp", trace_path=None)
        # Should not raise
        client._trace_log("send", {"test": True})


# ---------------------------------------------------------------------------
# Phase 2.1: Persistent Index + Incremental Updates
# ---------------------------------------------------------------------------


class TestPersistentIndex:
    def test_file_metadata_crud(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()

        # Insert
        result = store.upsert_file_metadata("src/main.py", 1000.0, 500, 10, 1234)
        assert isinstance(result, Ok)

        # Get
        meta = store.get_file_metadata("src/main.py")
        assert isinstance(meta, Ok)
        assert meta.value is not None
        assert meta.value["mtime"] == 1000.0
        assert meta.value["size"] == 500

        # Get all
        all_meta = store.get_all_file_metadata()
        assert isinstance(all_meta, Ok)
        assert "src/main.py" in all_meta.value

        # Delete
        del_result = store.delete_file_metadata("src/main.py")
        assert isinstance(del_result, Ok)

        meta2 = store.get_file_metadata("src/main.py")
        assert isinstance(meta2, Ok)
        assert meta2.value is None

    def test_file_metadata_not_found(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()

        result = store.get_file_metadata("nonexistent.py")
        assert isinstance(result, Ok)
        assert result.value is None


# ---------------------------------------------------------------------------
# Phase 3.1: Crash Recovery
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    @pytest.mark.asyncio
    async def test_poison_file_skipped_after_two_failures(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()

        indexer = Indexer(store, MagicMock(spec=LSPManager))

        # Manually mark file as poison (simulates 2 prior crashes)
        fp = "bad_file.py"
        indexer._crash_counts[fp] = 2
        indexer._poison_files.add(fp)

        r = await indexer.index_file(fp)
        assert isinstance(r, Err)
        assert r.error.code == "poison_file"

    @pytest.mark.asyncio
    async def test_crash_count_increments(self) -> None:
        """Document symbol failure increments crash count and marks poison at 2."""
        store = SymbolStore(":memory:")
        store.initialize()

        mock_client = AsyncMock()
        mock_client.is_running = True
        mock_client.did_open = AsyncMock()
        mock_client.drain = AsyncMock()
        mock_client.did_close = AsyncMock()
        mock_client.document_symbol = AsyncMock(
            return_value=Err(
                GroundTruthError(
                    code="lsp_timeout",
                    message="documentSymbol timed out",
                )
            )
        )

        mock_manager = MagicMock(spec=LSPManager)
        mock_manager.ensure_server = AsyncMock(return_value=Ok(mock_client))

        indexer = Indexer(store, mock_manager)

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False, mode="w") as f:
            f.write("const x = 1;\n")
            tmp_path = f.name

        try:
            # First failure
            r1 = await indexer.index_file(tmp_path)
            assert isinstance(r1, Err)
            norm = os.path.normpath(tmp_path).replace("\\", "/")
            assert indexer._crash_counts.get(norm) == 1

            # Second failure → should mark as poison
            r2 = await indexer.index_file(tmp_path)
            assert isinstance(r2, Err)
            assert norm in indexer._poison_files

            # Third call → immediately rejected
            r3 = await indexer.index_file(tmp_path)
            assert isinstance(r3, Err)
            assert r3.error.code == "poison_file"
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Phase 3.2: SQLite Error Handling
# ---------------------------------------------------------------------------


class TestSQLiteResilience:
    def test_rebuild_fts(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()

        now = int(time.time())
        store.insert_symbol(
            name="foo",
            kind="function",
            language="python",
            file_path="a.py",
            line_number=1,
            end_line=5,
            is_exported=True,
            signature="() -> None",
            params=None,
            return_type="None",
            documentation=None,
            last_indexed_at=now,
        )

        result = store.rebuild_fts()
        assert isinstance(result, Ok)

        # FTS should still work after rebuild
        search = store.search_symbols_fts("foo")
        assert isinstance(search, Ok)
        assert len(search.value) == 1

    def test_wal_checkpoint(self) -> None:
        store = SymbolStore(":memory:")
        result = store.initialize()
        assert isinstance(result, Ok)


# ---------------------------------------------------------------------------
# Phase 3.4: Path Sandboxing
# ---------------------------------------------------------------------------


class TestPathValidation:
    def test_valid_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "src", "main.py")
            os.makedirs(os.path.dirname(test_file), exist_ok=True)
            ok, _ = validate_path(test_file, tmpdir)
            assert ok is True

    def test_relative_path_under_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, _ = validate_path("src/main.py", tmpdir)
            assert ok is True

    def test_path_traversal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, msg = validate_path("../../etc/passwd", tmpdir)
            assert ok is False
            assert "escapes" in msg.lower() or "invalid" in msg.lower()

    def test_check_path_helper_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _check_path("../../etc/passwd", tmpdir)
            assert result is not None
            assert "error" in result

    def test_check_path_helper_returns_none_when_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _check_path("src/main.py", tmpdir)
            assert result is None

    def test_check_path_skips_when_no_root(self) -> None:
        result = _check_path("anything.py", None)
        assert result is None


# ---------------------------------------------------------------------------
# Phase 3.5: Prompt Sanitization
# ---------------------------------------------------------------------------


class TestSanitization:
    def test_normal_text_unchanged(self) -> None:
        text = "def getUserById(user_id: int) -> User"
        assert sanitize_for_prompt(text) == text

    def test_control_chars_stripped(self) -> None:
        text = "hello\x00world\x01test"
        result = sanitize_for_prompt(text)
        assert result == "helloworldtest"

    def test_newline_and_tab_preserved(self) -> None:
        text = "line1\nline2\ttab"
        assert sanitize_for_prompt(text) == text

    def test_truncation(self) -> None:
        text = "x" * 1000
        result = sanitize_for_prompt(text, max_length=100)
        assert len(result) == 100


# ---------------------------------------------------------------------------
# Phase 3.6: File Size + Symlink Guards
# ---------------------------------------------------------------------------


class TestFileGuards:
    @pytest.mark.asyncio
    async def test_large_file_skipped(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()

        mock_manager = MagicMock(spec=LSPManager)
        indexer = Indexer(store, mock_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a Python file larger than 100 bytes
            big_file = os.path.join(tmpdir, "big.py")
            with open(big_file, "w") as f:
                f.write("x = 1\n" * 100)

            result = await indexer.index_project(tmpdir, max_file_size=100)
            assert isinstance(result, Ok)
            # The big file should be skipped (no symbols)
            assert result.value == 0


# ---------------------------------------------------------------------------
# Phase 4.1: Meta-tool (groundtruth_do)
# ---------------------------------------------------------------------------


class TestOperationDetection:
    def test_explain_keywords(self) -> None:
        assert _detect_operation("how does auth work") == "explain"
        assert _detect_operation("what is getUserById") == "explain"
        assert _detect_operation("explain the login flow") == "explain"

    def test_validate_keywords(self) -> None:
        assert _detect_operation("validate this code") == "validate"
        assert _detect_operation("check for errors") == "validate"

    def test_trace_keywords(self) -> None:
        assert _detect_operation("trace getUserById") == "trace"
        assert _detect_operation("who calls this function") == "trace"

    def test_find_keywords(self) -> None:
        assert _detect_operation("find auth files") == "find"
        assert _detect_operation("where is the config") == "find"

    def test_default_is_explain(self) -> None:
        assert _detect_operation("random stuff here") == "explain"


class TestHandleDo:
    """Helper to build shared deps for handle_do tests."""

    @staticmethod
    def _make_deps(store: SymbolStore) -> dict[str, Any]:
        from groundtruth.ai.briefing import BriefingEngine
        from groundtruth.ai.task_parser import TaskParser
        from groundtruth.analysis.risk_scorer import RiskScorer
        from groundtruth.index.graph import ImportGraph
        from groundtruth.stats.tracker import InterventionTracker
        from groundtruth.validators.orchestrator import ValidationOrchestrator

        return {
            "store": store,
            "graph": ImportGraph(store),
            "tracker": InterventionTracker(store),
            "task_parser": TaskParser(store, api_key=None),
            "briefing_engine": BriefingEngine(store, api_key=None),
            "orchestrator": ValidationOrchestrator(store, api_key=None),
            "risk_scorer": RiskScorer(store),
        }

    @staticmethod
    def _seed_symbol(store: SymbolStore) -> None:
        now = int(time.time())
        store.insert_symbol(
            name="getUserById",
            kind="function",
            language="python",
            file_path="src/users.py",
            line_number=1,
            end_line=10,
            is_exported=True,
            signature="(id: int) -> User",
            params=None,
            return_type="User",
            documentation=None,
            last_indexed_at=now,
        )

    @pytest.mark.asyncio
    async def test_basic_find_pipeline(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()
        self._seed_symbol(store)
        deps = self._make_deps(store)

        result = await handle_do(
            query="find getUserById",
            operation="find",
            **deps,
        )

        assert "summary" in result
        assert "results" in result
        # New response shape: results is dict, pipeline key exists
        assert isinstance(result["results"], dict)
        assert "find" in result["pipeline"]

    @pytest.mark.asyncio
    async def test_short_circuit_on_empty(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()
        deps = self._make_deps(store)

        result = await handle_do(
            query="nonexistent symbol xyz",
            **deps,
        )

        assert "No relevant files" in result["summary"]
        # Pipeline should contain at least "find"
        assert "find" in result["pipeline"]

    @pytest.mark.asyncio
    async def test_explicit_steps(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()
        self._seed_symbol(store)
        deps = self._make_deps(store)

        result = await handle_do(
            query=None,
            steps=[{"tool": "find", "description": "getUserById"}, {"tool": "stats"}],
            **deps,
        )

        assert result["intent"] == "explicit"
        assert result["pipeline"] == ["find", "stats"]
        assert "find" in result["results"]
        assert "stats" in result["results"]

    @pytest.mark.asyncio
    async def test_query_xor_steps(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()
        deps = self._make_deps(store)

        result = await handle_do(
            query="some query",
            steps=[{"tool": "find"}],
            **deps,
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_scope_filtering(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()

        now = int(time.time())
        # Add symbols in different directories
        for name, fp in [("funcA", "src/api/a.py"), ("funcB", "src/utils/b.py")]:
            store.insert_symbol(
                name=name,
                kind="function",
                language="python",
                file_path=fp,
                line_number=1,
                end_line=5,
                is_exported=True,
                signature="() -> None",
                params=None,
                return_type="None",
                documentation=None,
                last_indexed_at=now,
            )

        deps = self._make_deps(store)

        result = await handle_do(
            query="find funcA funcB",
            scope="src/api",
            **deps,
        )

        find_result = result["results"].get("find", {})
        files = find_result.get("files", [])
        # Only files under src/api should survive
        for f in files:
            assert f["path"].startswith("src/api")

    @pytest.mark.asyncio
    async def test_depth_quick(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()
        self._seed_symbol(store)
        deps = self._make_deps(store)

        result = await handle_do(
            query="find getUserById",
            depth="quick",
            **deps,
        )

        assert result["pipeline"] == ["find"]

    @pytest.mark.asyncio
    async def test_depth_deep(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()
        self._seed_symbol(store)
        deps = self._make_deps(store)

        result = await handle_do(
            query="getUserById",
            depth="deep",
            file_path="src/users.py",
            code="from users import getUserById",
            **deps,
        )

        assert result["pipeline"] == ["find", "brief", "validate", "trace"]

    @pytest.mark.asyncio
    async def test_deps_combined(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()
        deps = self._make_deps(store)

        result = await handle_do(
            query=None,
            steps=[{"tool": "deps"}],
            **deps,
        )

        deps_result = result["results"].get("deps", {})
        assert "dead_code" in deps_result
        assert "unused_packages" in deps_result

    @pytest.mark.asyncio
    async def test_result_forwarding(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()
        self._seed_symbol(store)
        deps = self._make_deps(store)

        result = await handle_do(
            query="getUserById",
            operation="trace",
            **deps,
        )

        # Pipeline should be find → trace (from intent override)
        assert result["pipeline"] == ["find", "trace"]
        # Trace should have run (symbol forwarded from find)
        assert "trace" in result["results"]

    @pytest.mark.asyncio
    async def test_backward_compat_operation(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()
        self._seed_symbol(store)
        deps = self._make_deps(store)

        result = await handle_do(
            query="getUserById",
            operation="trace",
            **deps,
        )

        assert result["intent"] == "trace"
        assert "find" in result["pipeline"]
        assert "trace" in result["pipeline"]


# ---------------------------------------------------------------------------
# Phase 5.2: ToolResponse Builder
# ---------------------------------------------------------------------------


class TestToolResponse:
    def test_basic_build(self) -> None:
        resp = ToolResponse()
        resp.set("valid", True).set("errors", [])
        resp.add_guidance("Check callers before editing.")

        result = resp.build()
        assert result["valid"] is True
        assert result["errors"] == []
        assert "latency_ms" in result
        assert "reasoning_guidance" in result

    def test_error_shortcut(self) -> None:
        resp = ToolResponse()
        result = resp.error("something broke")
        assert result == {"error": "something broke"}

    def test_timing(self) -> None:
        resp = ToolResponse()
        result = resp.build()
        assert result["latency_ms"] >= 1


# ---------------------------------------------------------------------------
# Phase 6.1: Setup Command
# ---------------------------------------------------------------------------


class TestSetupCmd:
    def test_setup_cmd_runs(self) -> None:
        from groundtruth.cli.commands import setup_cmd

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a .py file
            with open(os.path.join(tmpdir, "test.py"), "w") as f:
                f.write("x = 1\n")
            # Should not raise
            setup_cmd(tmpdir)


# ---------------------------------------------------------------------------
# Phase 2.2: Parallel Indexing
# ---------------------------------------------------------------------------


class TestParallelIndexing:
    @pytest.mark.asyncio
    async def test_concurrency_parameter(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()

        mock_manager = MagicMock(spec=LSPManager)
        indexer = Indexer(store, mock_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create multiple small Python files
            for i in range(5):
                with open(os.path.join(tmpdir, f"mod_{i}.py"), "w") as f:
                    f.write(f"x_{i} = {i}\n")

            # With concurrency=2 (tests that the param doesn't crash)
            result = await indexer.index_project(tmpdir, concurrency=2, max_file_size=1_048_576)
            assert isinstance(result, Ok)

    @pytest.mark.asyncio
    async def test_force_reindex(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()

        # Seed metadata
        store.upsert_file_metadata("src/main.py", 1000.0, 50, 5, int(time.time()))

        mock_manager = MagicMock(spec=LSPManager)
        indexer = Indexer(store, mock_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            # No Python files → should still complete
            result = await indexer.index_project(tmpdir, force=True)
            assert isinstance(result, Ok)
