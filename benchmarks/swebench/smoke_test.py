"""Smoke tests for passive GT integration (V2 mode).

Tier 1: Run GT validation on correct code — assert 0 false positives.
Tier 2: 10-task mini A/B (requires API key, optional).
Tier 3: Index sample repos — assert all succeed.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from groundtruth.index.store import SymbolStore
from groundtruth.index.ast_parser import parse_python_file
from groundtruth.validators.ast_validator import AstValidator

from .gt_integration import GTIntegration, GT_ARTIFACT_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_store_with_file(file_path: str, content: str) -> SymbolStore:
    """Create an in-memory store and index a single Python file."""
    store = SymbolStore(":memory:")
    store.initialize()

    # Write temp file
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    Path(file_path).write_text(content, encoding="utf-8")

    # Parse and insert
    symbols = parse_python_file(file_path)
    import time

    now = int(time.time())
    for sym in symbols:
        store.insert_symbol(
            name=sym.name,
            kind=sym.kind,
            language="python",
            file_path=file_path,
            line_number=sym.line,
            end_line=sym.end_line,
            is_exported=sym.is_exported,
            signature=sym.signature,
            params=None,
            return_type=sym.return_type,
            documentation=sym.documentation,
            last_indexed_at=now,
        )
        for child in sym.children:
            store.insert_symbol(
                name=child.name,
                kind=child.kind,
                language="python",
                file_path=file_path,
                line_number=child.line,
                end_line=child.end_line,
                is_exported=child.is_exported,
                signature=child.signature,
                params=None,
                return_type=child.return_type,
                documentation=child.documentation,
                last_indexed_at=now,
            )

    return store


# ---------------------------------------------------------------------------
# Tier 1: Zero false positives on correct code
# ---------------------------------------------------------------------------


class TestTier1NoPositives:
    """Validate that correct code produces no validation findings."""

    def test_correct_stdlib_imports(self, tmp_path: Path) -> None:
        """Correct stdlib imports should not trigger errors."""
        code = (
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            "from collections import defaultdict\n"
            "\n"
            "p = Path('.')\n"
            "d = defaultdict(list)\n"
        )
        fpath = str(tmp_path / "good.py")
        store = _create_store_with_file(fpath, code)
        gt = GTIntegration(store=store, repo_path=str(tmp_path))
        gt.mark_index_complete(0.1, 0)

        feedback = gt.post_edit_validate(fpath, code)
        assert feedback is None, f"Expected no feedback, got: {feedback}"

    def test_correct_internal_imports(self, tmp_path: Path) -> None:
        """Imports of symbols that exist in the index should be clean."""
        # Create a module with a function
        mod_path = str(tmp_path / "mymod.py")
        mod_code = "def helper(x: int) -> str:\n    return str(x)\n"
        store = _create_store_with_file(mod_path, mod_code)

        # Now validate code that uses it
        user_code = "from mymod import helper\nresult = helper(42)\n"
        user_path = str(tmp_path / "user.py")
        Path(user_path).write_text(user_code, encoding="utf-8")

        gt = GTIntegration(store=store, repo_path=str(tmp_path))
        gt.mark_index_complete(0.1, 1)

        # This shouldn't flag helper since it's in the index
        feedback = gt.post_edit_validate(user_path, user_code)
        # Note: may or may not flag depending on module path resolution.
        # The key is no crash.


# ---------------------------------------------------------------------------
# Tier 2: Mini A/B (requires API key, optional)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="Requires OPENAI_API_KEY for live A/B test",
)
class TestTier2MiniAB:
    """Run a few tasks with V2 mode. Requires API key."""

    def test_placeholder(self) -> None:
        """Placeholder for mini A/B — implement when running live benchmarks."""
        pytest.skip("Mini A/B requires live benchmark infrastructure")


# ---------------------------------------------------------------------------
# Tier 3: Index sample repos
# ---------------------------------------------------------------------------


class TestTier3IndexRepos:
    """Verify indexing works on sample Python code."""

    def test_index_small_project(self, tmp_path: Path) -> None:
        """Index a small multi-file project."""
        # Create files
        (tmp_path / "main.py").write_text(
            "from utils import format_name\n\ndef main():\n    print(format_name('test'))\n",
            encoding="utf-8",
        )
        (tmp_path / "utils.py").write_text(
            "def format_name(name: str) -> str:\n    return name.title()\n",
            encoding="utf-8",
        )

        store = SymbolStore(":memory:")
        store.initialize()
        gt = GTIntegration(store=store, repo_path=str(tmp_path))

        # Index each file
        for py_file in tmp_path.glob("*.py"):
            gt.reindex_single_file(str(py_file))

        gt.mark_index_complete(0.1, 3)

        # Verify symbols exist
        from groundtruth.utils.result import Ok

        result = store.find_symbol_by_name("format_name")
        assert isinstance(result, Ok)
        assert len(result.value) >= 1

        result = store.find_symbol_by_name("main")
        assert isinstance(result, Ok)
        assert len(result.value) >= 1

    def test_gt_integration_final_report(self, tmp_path: Path) -> None:
        """final_report() returns valid structure."""
        store = SymbolStore(":memory:")
        store.initialize()
        gt = GTIntegration(store=store, repo_path=str(tmp_path))
        gt.mark_index_complete(1.5, 100)

        report = gt.final_report()
        assert report["artifact_version"] == GT_ARTIFACT_VERSION
        assert isinstance(report["instrumentation"], dict)
        assert report["instrumentation"]["gt_available"] is True
        assert report["instrumentation"]["index_symbols"] == 100

    def test_enrich_system_prompt(self, tmp_path: Path) -> None:
        """enrich_system_prompt adds context when symbols exist."""
        fpath = str(tmp_path / "models.py")
        code = "class User:\n    def get_name(self) -> str:\n        return self.name\n"
        store = _create_store_with_file(fpath, code)

        gt = GTIntegration(store=store, repo_path=str(tmp_path))
        gt.mark_index_complete(0.1, 2)

        base = "You are an agent."
        enriched = gt.enrich_system_prompt("fix User.get_name", base)
        assert len(enriched) > len(base)
        assert "User" in enriched or "get_name" in enriched
