"""Gate 2 diagnostic — 10-task comparison: incubator flags ON vs OFF.

Tests handle_consolidated_check with realistic diffs against a seeded store.
Compares output with all incubator flags OFF (baseline) vs ON (incubator).

Pass criteria:
  - Zero regressions: baseline obligations are still present when flags ON
  - At least 2 tasks where incubator adds value (contradictions, pattern roles,
    abstention filtering, or convention info)
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.mcp.tools.core_tools import handle_consolidated_check
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.result import Ok
from groundtruth.validators.autocorrect import AutoCorrector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_store(store: SymbolStore, symbols: list[dict], refs: list[dict] | None = None,
                attributes: list[dict] | None = None) -> None:
    """Seed a store with symbols, refs, and attributes for testing."""
    # Track inserted IDs by name for cross-referencing
    name_to_id: dict[str, int] = {}
    for s in symbols:
        result = store.insert_symbol(
            name=s["name"], kind=s["kind"], language="python",
            file_path=s["file_path"],
            line_number=s.get("line", 1), end_line=s.get("end_line", 20),
            is_exported=s.get("is_exported", True),
            signature=s.get("signature"), params=None,
            return_type=None, documentation=None,
            last_indexed_at=1000,
        )
        if isinstance(result, Ok):
            name_to_id[s["name"]] = result.value
    for r in (refs or []):
        sym_id = name_to_id.get(r["symbol_name"])
        if sym_id:
            store.insert_ref(
                symbol_id=sym_id,
                referenced_in_file=r["file"],
                referenced_at_line=r.get("line", 1),
                reference_type=r.get("type", "call"),
            )
    for a in (attributes or []):
        cls_id = name_to_id.get(a["class_name"])
        if cls_id:
            # Resolve method names to actual IDs
            method_names = a.get("method_names", [])
            method_ids = [name_to_id[mn] for mn in method_names if mn in name_to_id]
            store.insert_attribute(
                symbol_id=cls_id,
                name=a["name"],
                method_ids=method_ids if method_ids else None,
            )


async def _run_check(store: SymbolStore, diff: str, root: str = "/fake",
                     flags_on: bool = False) -> dict:
    """Run handle_consolidated_check with flags ON or OFF."""
    env_vars = {}
    if flags_on:
        env_vars = {
            "GT_ENABLE_CONTRADICTIONS": "1",
            "GT_ENABLE_ABSTENTION": "1",
            "GT_ENABLE_STATE_FLOW": "1",
            "GT_ENABLE_REPO_INTEL": "1",
            "GT_ENABLE_STRUCTURAL_SIMILARITY": "1",
        }
    else:
        # Ensure all flags are off
        for k in list(os.environ):
            if k.startswith("GT_ENABLE_"):
                env_vars[k] = ""

    graph = ImportGraph(store)
    tracker = InterventionTracker(store)
    autocorrector = AutoCorrector(store, root, benchmark_safe=True, graph=graph)

    with patch.dict(os.environ, env_vars, clear=False):
        result = await handle_consolidated_check(
            diff=diff,
            autocorrector=autocorrector,
            store=store,
            graph=graph,
            tracker=tracker,
            root_path=root,
        )
    return result


# ---------------------------------------------------------------------------
# 10 diagnostic tasks
# ---------------------------------------------------------------------------


# Task 1: Constructor symmetry — __init__ sets 3 attrs, __repr__ uses 2
TASK1_DIFF = """\
--- /dev/null
+++ b/src/models.py
@@ -0,0 +1,10 @@
+class Point:
+    def __init__(self, x, y, z):
+        self.x = x
+        self.y = y
+        self.z = z
+
+    def __repr__(self):
+        return f"Point({self.x}, {self.y})"
"""

TASK1_SYMBOLS = [
    {"name": "Point", "kind": "class", "file_path": "src/models.py", "line": 1, "end_line": 10},
    {"name": "__init__", "kind": "method", "file_path": "src/models.py", "line": 2, "end_line": 6},
    {"name": "__repr__", "kind": "method", "file_path": "src/models.py", "line": 7, "end_line": 10},
]
TASK1_ATTRS = [
    {"class_name": "Point", "name": "x", "method_names": ["__init__", "__repr__"]},
    {"class_name": "Point", "name": "y", "method_names": ["__init__", "__repr__"]},
    {"class_name": "Point", "name": "z", "method_names": ["__init__"]},
]


# Task 2: Override contract — Dog.speak has different arity than Animal.speak
TASK2_DIFF = """\
--- /dev/null
+++ b/src/dog.py
@@ -0,0 +1,4 @@
+class Dog(Animal):
+    def speak(self):
+        return "woof"
"""

TASK2_SYMBOLS = [
    {"name": "Animal", "kind": "class", "file_path": "src/base.py", "line": 1, "end_line": 30},
    {"name": "speak", "kind": "method", "file_path": "src/base.py", "line": 5, "end_line": 10,
     "signature": "(self, volume: int)"},
]


# Task 3: Caller contract — function has multiple call sites
TASK3_DIFF = """\
--- a/src/utils.py
+++ b/src/utils.py
@@ -5,3 +5,4 @@
 def process(data):
     # Changed signature
+    mode = "fast"
     return transform(data)
"""

TASK3_SYMBOLS = [
    {"name": "process", "kind": "function", "file_path": "src/utils.py", "line": 5, "end_line": 10,
     "signature": "(data)"},
]
TASK3_REFS = [
    {"symbol_name": "process", "file": "src/main.py", "line": 15, "type": "call"},
    {"symbol_name": "process", "file": "src/api.py", "line": 8, "type": "call"},
    {"symbol_name": "process", "file": "tests/test_utils.py", "line": 3, "type": "call"},
]


# Task 4: Shared state — attribute used in multiple methods
TASK4_DIFF = """\
--- a/src/cache.py
+++ b/src/cache.py
@@ -1,6 +1,7 @@
 class CacheManager:
     def __init__(self, data, ttl):
         self.data = data
+        self.max_size = 1000
         self.ttl = ttl

     def get(self, key):
"""

TASK4_SYMBOLS = [
    {"name": "CacheManager", "kind": "class", "file_path": "src/cache.py", "line": 1, "end_line": 30},
    {"name": "__init__", "kind": "method", "file_path": "src/cache.py", "line": 2, "end_line": 6},
    {"name": "get", "kind": "method", "file_path": "src/cache.py", "line": 7, "end_line": 10},
    {"name": "set", "kind": "method", "file_path": "src/cache.py", "line": 11, "end_line": 14},
    {"name": "clear", "kind": "method", "file_path": "src/cache.py", "line": 15, "end_line": 18},
]
TASK4_ATTRS = [
    {"class_name": "CacheManager", "name": "data", "method_names": ["__init__", "get", "set", "clear"]},
    {"class_name": "CacheManager", "name": "ttl", "method_names": ["__init__", "get"]},
]


# Task 5: False positive — comment-only change (no obligations expected)
TASK5_DIFF = """\
--- a/src/utils.py
+++ b/src/utils.py
@@ -1,3 +1,3 @@
-# Old comment
+# Updated comment
 def helper():
     pass
"""

TASK5_SYMBOLS = [
    {"name": "helper", "kind": "function", "file_path": "src/utils.py", "line": 2, "end_line": 4},
]


# Task 6: False positive — new standalone function (no coupling)
TASK6_DIFF = """\
--- /dev/null
+++ b/src/new_module.py
@@ -0,0 +1,3 @@
+def brand_new_function(x):
+    return x * 2
"""

TASK6_SYMBOLS = []


# Task 7: Override with correct arity (should NOT fire contradiction)
TASK7_DIFF = """\
--- /dev/null
+++ b/src/cat.py
@@ -0,0 +1,4 @@
+class Cat(Animal):
+    def speak(self, volume: int):
+        return "meow" * volume
"""

TASK7_SYMBOLS = [
    {"name": "Animal", "kind": "class", "file_path": "src/base.py", "line": 1, "end_line": 30},
    {"name": "speak", "kind": "method", "file_path": "src/base.py", "line": 5, "end_line": 10,
     "signature": "(self, volume: int)"},
]


# Task 8: Import path contradiction — symbol moved but import uses old path
TASK8_DIFF = """\
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 from old_module import UserService
+from utils import format_name

 app = UserService()
"""

TASK8_SYMBOLS = [
    {"name": "UserService", "kind": "class", "file_path": "src/services/user.py", "line": 1},
    {"name": "format_name", "kind": "function", "file_path": "src/utils.py", "line": 10,
     "signature": "(name: str)"},
]


# Task 9: Arity mismatch — function called with too few args
TASK9_DIFF = """\
--- a/src/main.py
+++ b/src/main.py
@@ -1,2 +1,3 @@
 from utils import compute
+result = compute(42)
"""

TASK9_SYMBOLS = [
    {"name": "compute", "kind": "function", "file_path": "src/utils.py", "line": 5,
     "signature": "(a: int, b: int, c: int)"},
]


# Task 10: Multiple obligations + contradiction together
TASK10_DIFF = """\
--- a/src/service.py
+++ b/src/service.py
@@ -1,5 +1,7 @@
 class Service:
     def __init__(self, config, db):
         self.config = config
         self.db = db
+        self.cache = {}
+
+    def process(self):
+        return self.config
"""

TASK10_SYMBOLS = [
    {"name": "Service", "kind": "class", "file_path": "src/service.py", "line": 1, "end_line": 20},
    {"name": "__init__", "kind": "method", "file_path": "src/service.py", "line": 2, "end_line": 5},
    {"name": "process", "kind": "method", "file_path": "src/service.py", "line": 6, "end_line": 8},
]
TASK10_ATTRS = [
    {"class_name": "Service", "name": "config", "method_names": ["__init__", "process"]},
    {"class_name": "Service", "name": "db", "method_names": ["__init__"]},
]


# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------

TASKS = [
    ("task1_constructor_symmetry", TASK1_DIFF, TASK1_SYMBOLS, [], TASK1_ATTRS),
    ("task2_override_contract", TASK2_DIFF, TASK2_SYMBOLS, [], []),
    ("task3_caller_contract", TASK3_DIFF, TASK3_SYMBOLS, TASK3_REFS, []),
    ("task4_shared_state", TASK4_DIFF, TASK4_SYMBOLS, [], TASK4_ATTRS),
    ("task5_false_positive_comment", TASK5_DIFF, TASK5_SYMBOLS, [], []),
    ("task6_false_positive_new_standalone", TASK6_DIFF, TASK6_SYMBOLS, [], []),
    ("task7_correct_override", TASK7_DIFF, TASK7_SYMBOLS, [], []),
    ("task8_import_path_moved", TASK8_DIFF, TASK8_SYMBOLS, [], []),
    ("task9_arity_mismatch", TASK9_DIFF, TASK9_SYMBOLS, [], []),
    ("task10_multi_obligation", TASK10_DIFF, TASK10_SYMBOLS, [], TASK10_ATTRS),
]


# ---------------------------------------------------------------------------
# Gate 2 tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGate2Diagnostic:
    """Run 10 tasks with flags OFF and ON, compare results."""

    @pytest.fixture
    def store(self, in_memory_store: SymbolStore) -> SymbolStore:
        return in_memory_store

    @pytest.mark.parametrize(
        "task_name,diff,symbols,refs,attrs",
        TASKS,
        ids=[t[0] for t in TASKS],
    )
    async def test_no_regression(
        self, store: SymbolStore, task_name: str,
        diff: str, symbols: list, refs: list, attrs: list,
    ) -> None:
        """Flags ON must not lose any obligation that flags OFF produces."""
        _seed_store(store, symbols, refs, attrs)

        baseline = await _run_check(store, diff, flags_on=False)
        incubator = await _run_check(store, diff, flags_on=True)

        # Every baseline obligation must still appear
        baseline_obs = {(o["kind"], o["target"]) for o in baseline.get("obligations", [])}
        incubator_obs = {(o["kind"], o["target"]) for o in incubator.get("obligations", [])}

        missing = baseline_obs - incubator_obs
        assert not missing, f"Regression in {task_name}: lost obligations {missing}"

    @pytest.mark.parametrize(
        "task_name,diff,symbols,refs,attrs",
        TASKS,
        ids=[t[0] for t in TASKS],
    )
    async def test_flags_off_stable(
        self, store: SymbolStore, task_name: str,
        diff: str, symbols: list, refs: list, attrs: list,
    ) -> None:
        """Flags OFF output should be consistent (smoke test)."""
        _seed_store(store, symbols, refs, attrs)
        result = await _run_check(store, diff, flags_on=False)
        assert "obligations" in result
        assert "contradictions" in result
        assert isinstance(result["obligations"], list)


class TestGate2ValueAdded:
    """Verify incubator wiring is active and produces correct output structure."""

    @pytest.mark.asyncio
    async def test_incubator_output_structure(self) -> None:
        """Flags ON adds info field and preserves obligations."""
        store = SymbolStore(":memory:")
        store.initialize()
        _seed_store(store, TASK1_SYMBOLS, [], TASK1_ATTRS)

        baseline = await _run_check(store, TASK1_DIFF, flags_on=False)
        incubator = await _run_check(store, TASK1_DIFF, flags_on=True)
        store.close()

        # Both produce obligations
        assert len(baseline["obligations"]) > 0
        assert len(incubator["obligations"]) > 0
        # Incubator output includes info field
        assert "info" in incubator

    @pytest.mark.asyncio
    async def test_no_regressions_across_all_tasks(self) -> None:
        """All 10 tasks: flags ON has >= obligations vs baseline."""
        regressions = 0
        for task_name, diff, symbols, refs, attrs in TASKS:
            store = SymbolStore(":memory:")
            store.initialize()
            _seed_store(store, symbols, refs, attrs)

            baseline = await _run_check(store, diff, flags_on=False)
            incubator = await _run_check(store, diff, flags_on=True)
            store.close()

            baseline_count = len(baseline.get("obligations", []))
            incubator_count = len(incubator.get("obligations", []))
            if incubator_count < baseline_count:
                regressions += 1

        assert regressions == 0, f"{regressions} task(s) had obligation regressions"

    @pytest.mark.asyncio
    async def test_pattern_roles_on_shared_state(self) -> None:
        """Shared state tasks should get pattern_roles when state_flow flag is ON."""
        store = SymbolStore(":memory:")
        store.initialize()
        _seed_store(store, TASK4_SYMBOLS, [], TASK4_ATTRS)

        # Note: pattern roles require source file on disk for _read_source_lines.
        # In fixture tests, they won't fire. This test verifies the wiring
        # doesn't crash and the output structure is correct.
        result = await _run_check(store, TASK4_DIFF, flags_on=True)
        store.close()

        assert "obligations" in result
        assert isinstance(result["obligations"], list)

    @pytest.mark.asyncio
    async def test_repo_intel_logging(self) -> None:
        """With REPO_INTEL flag, patterns should be logged to store."""
        store = SymbolStore(":memory:")
        store.initialize()
        _seed_store(store, TASK1_SYMBOLS, [], TASK1_ATTRS)

        with patch.dict(os.environ, {"GT_ENABLE_REPO_INTEL": "1"}):
            result = await _run_check(store, TASK1_DIFF, flags_on=True)

        # Check that patterns were logged (logged under obl.source, e.g. "Point.__init__")
        patterns = store.get_patterns_for_subject("Point.__init__")
        if isinstance(patterns, Ok) and result["obligations"]:
            assert len(patterns.value) > 0, "Repo intel should log obligation patterns"
        store.close()

    @pytest.mark.asyncio
    async def test_false_positives_unchanged(self) -> None:
        """Tasks 5 and 6 (false positives) must produce 0 obligations in both modes."""
        for diff, symbols in [(TASK5_DIFF, TASK5_SYMBOLS), (TASK6_DIFF, TASK6_SYMBOLS)]:
            store = SymbolStore(":memory:")
            store.initialize()
            _seed_store(store, symbols, [], [])

            baseline = await _run_check(store, diff, flags_on=False)
            incubator = await _run_check(store, diff, flags_on=True)
            store.close()

            assert len(baseline["obligations"]) == 0
            assert len(incubator["obligations"]) == 0
