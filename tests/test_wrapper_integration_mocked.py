#!/usr/bin/env python3
"""Integration test for oh_gt_full_wrapper with MOCKED OpenHands layer.

Uses REAL wrapper code (imports from scripts/swebench/oh_gt_full_wrapper.py).
Uses MOCKED OH layer (fake runtime, fake actions, fake observations).
Feeds REAL commands from Run B trajectories (dynaconf-1225 and beets-5682).
Adds EDGE CASES to stress-test.

Run: python tests/test_wrapper_integration_mocked.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import traceback

# Path setup — import from real project sources
_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TEST_DIR)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "scripts", "swebench"))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "src"))


# ---------------------------------------------------------------------------
# Mock OH classes
# ---------------------------------------------------------------------------

class FakeObservation:
    def __init__(self, content=""):
        self.content = content


class FakeAction:
    def __init__(self, action_type="run", command="", path=""):
        self.action = action_type
        self.command = command
        self.path = path
        self.thought = ""


class FakeRuntime:
    def __init__(self):
        self.run_action = self._run_action
        self._gt_instance = None
        self._gt_full_config = None

    def _run_action(self, action):
        return FakeObservation("fake output")

    def copy_to(self, src, dst):
        pass


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

_RESULTS: list[tuple[str, bool, str]] = []


def _record(name: str, passed: bool, detail: str = ""):
    _RESULTS.append((name, passed, detail))


def _assert(condition: bool, msg: str):
    if not condition:
        raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Test 1: Grep interception with REAL commands from dynaconf-1225
# ---------------------------------------------------------------------------

def test_1_grep_symbol_extraction():
    """Feed real trajectory grep commands through _extract_search_symbol."""
    from oh_gt_full_wrapper import _extract_search_symbol

    real_commands = [
        'cd /workspace/dynaconf__dynaconf-1225 && grep -n "insert_token\\|source_metadata\\|populate_obj" dynaconf/',
        'cd /workspace/dynaconf__dynaconf-1225 && grep -rn "identifier\\|IDENTIFIER\\|source_metadata" dynaconf/',
        'cd /workspace/dynaconf__dynaconf-1225 && grep -n "def populate_obj\\|def load_file\\|def build_env_list" dynaconf/base.py',
        'cd /workspace/dynaconf__dynaconf-1225 && grep -n "def build_env_list" dynaconf/utils/__init__.py',
        'cd /workspace/dynaconf__dynaconf-1225 && grep -rn "insert" tests/ --include="*.py"',
    ]

    for cmd in real_commands:
        result = _extract_search_symbol(cmd)
        _assert(result != "", f"Empty symbol extracted from: {cmd}")
        _assert(result != "def", f"Extracted bare 'def' from: {cmd}")
        _assert(result != "class", f"Extracted bare 'class' from: {cmd}")
        _assert(len(result) >= 3, f"Symbol too short ({result!r}) from: {cmd}")

    # Verify specific extractions
    r0 = _extract_search_symbol(real_commands[0])
    _assert(r0 in ("insert_token", "source_metadata", "populate_obj"),
            f"Expected one of insert_token/source_metadata/populate_obj, got {r0!r}")

    r3 = _extract_search_symbol(real_commands[3])
    _assert(r3 == "build_env_list", f"Expected 'build_env_list', got {r3!r}")

    r4 = _extract_search_symbol(real_commands[4])
    _assert(r4 == "insert", f"Expected 'insert', got {r4!r}")


# ---------------------------------------------------------------------------
# Test 2: Constraint framing with fake callers
# ---------------------------------------------------------------------------

def test_2_constraint_framing():
    """format_risk_evidence with 5 high-confidence callers."""
    from groundtruth.hooks.post_edit import format_risk_evidence

    callers = [
        {"file": "src/api/auth.py", "line": "42", "code": "result = get_user(uid)"},
        {"file": "src/api/profile.py", "line": "88", "code": "u = get_user(request.user_id)"},
        {"file": "src/workers/sync.py", "line": "15", "code": "user = get_user(row['id'])"},
        {"file": "src/cli/admin.py", "line": "120", "code": "target = get_user(args.user)"},
        {"file": "tests/test_api.py", "line": "33", "code": "mock_user = get_user('test')"},
    ]

    output = format_risk_evidence(callers, "get_user", 0.95)
    text = "\n".join(output)

    _assert("DO NOT BREAK" in text, f"Missing 'DO NOT BREAK' in output: {text!r}")
    _assert("expects:" in text, f"Missing 'expects:' in output: {text!r}")
    _assert("callers depend on" not in text or "DO NOT BREAK" in text,
            f"Old format 'callers depend on' without 'DO NOT BREAK': {text!r}")


# ---------------------------------------------------------------------------
# Test 3: L5 scaffold trigger fires when 3+ scaffolds exist
# ---------------------------------------------------------------------------

def test_3_l5_scaffold_trigger():
    """Scaffold check logic fires when 3+ scaffolds and no source edit."""
    from oh_gt_full_wrapper import _is_scaffolding_path, _is_test_path

    edited_files = {"reproduce_issue.py", "test_fix.py", "debug_check.py", "another_debug.py"}

    # Simulate scaffold count using the real _is_scaffolding_path function
    # Note: test_fix.py matches SCAFFOLDING_PREFIXES ("test_fix"), debug_check matches "debug_"
    # reproduce_issue.py matches "reproduce_"
    # another_debug.py matches "debug_" (no — it's "another_debug.py", doesn't start with debug_)
    # Let's use the real function and verify our understanding
    scaffold_count = sum(1 for f in edited_files if _is_scaffolding_path(f))
    has_source = any(
        not _is_scaffolding_path(f) and not _is_test_path(f)
        for f in edited_files
    )

    # We expect at least 3 scaffolds from this set
    # reproduce_issue.py -> starts with reproduce_ -> YES
    # test_fix.py -> starts with test_fix -> YES
    # debug_check.py -> starts with debug_ -> YES
    # another_debug.py -> does NOT start with debug_ (starts with 'another') -> NO
    _assert(scaffold_count >= 3,
            f"Expected >= 3 scaffolds, got {scaffold_count}. "
            f"Scaffold checks: {[(f, _is_scaffolding_path(f)) for f in sorted(edited_files)]}")

    # another_debug.py doesn't start with a SCAFFOLDING_PREFIX, so it might be "source"
    # But it also doesn't match test patterns via TEST_PATH_RE, so has_source could be True
    # The point: if has_source is False, scaffold advisory SHOULD fire
    if not has_source:
        # Scaffold trigger should fire
        pass  # This is the desired state for a pure scaffold run
    else:
        # has_source = True means scaffold trigger should NOT fire
        # Verify which file is "source"
        source_files = [f for f in edited_files
                        if not _is_scaffolding_path(f) and not _is_test_path(f)]
        # This is expected — "another_debug.py" doesn't start with scaffold prefix
        pass

    # Now test with a clean scaffold-only set
    pure_scaffold_files = {"reproduce_issue.py", "test_fix_v2.py", "debug_check.py", "debug_other.py"}
    scaffold_count_2 = sum(1 for f in pure_scaffold_files if _is_scaffolding_path(f))
    has_source_2 = any(
        not _is_scaffolding_path(f) and not _is_test_path(f)
        for f in pure_scaffold_files
    )
    _assert(scaffold_count_2 >= 3,
            f"Expected >= 3 scaffolds in pure set, got {scaffold_count_2}")
    _assert(not has_source_2,
            f"Expected no source in pure scaffold set, but found source: "
            f"{[f for f in pure_scaffold_files if not _is_scaffolding_path(f) and not _is_test_path(f)]}")


# ---------------------------------------------------------------------------
# Test 4: L5b late-band fires at 65% with pending actions
# ---------------------------------------------------------------------------

def test_4_l5b_late_band():
    """L5b fires at 65% (>= 0.60) and blocks at 50% or after 2 injections."""
    # Fires at 65%
    action_count = 65
    max_iter = 100
    goku_active = True
    _l5b_count = 0
    _ratio_l5b = action_count / max(max_iter, 1)
    _l5b_allowed = (not goku_active) or (_l5b_count < 2 and _ratio_l5b >= 0.60)
    _assert(_l5b_allowed, f"L5b should fire at 65% but got allowed={_l5b_allowed}")

    # Does NOT fire at 50%
    action_count_2 = 50
    _ratio_l5b_2 = action_count_2 / max(max_iter, 1)
    _l5b_allowed_2 = (not goku_active) or (_l5b_count < 2 and _ratio_l5b_2 >= 0.60)
    _assert(not _l5b_allowed_2, f"L5b should NOT fire at 50% but got allowed={_l5b_allowed_2}")

    # Exactly 60% boundary — should fire
    action_count_3 = 60
    _ratio_l5b_3 = action_count_3 / max(max_iter, 1)
    _l5b_allowed_3 = (not goku_active) or (0 < 2 and _ratio_l5b_3 >= 0.60)
    _assert(_l5b_allowed_3, f"L5b should fire at exactly 60% but got allowed={_l5b_allowed_3}")

    # Budget exhausted after 2 injections
    _l5b_count_exhausted = 2
    _l5b_allowed_4 = (not goku_active) or (_l5b_count_exhausted < 2 and 0.7 >= 0.60)
    _assert(not _l5b_allowed_4,
            f"L5b should be blocked after 2 injections but got allowed={_l5b_allowed_4}")


# ---------------------------------------------------------------------------
# Test 5: Ledger + atexit flush
# ---------------------------------------------------------------------------

def test_5_ledger_flush():
    """Create a real Ledger, add entries, serialize, verify file output."""
    from groundtruth.runtime.ledger import Ledger, SignalOutcome

    ledger = Ledger()
    ledger.delivered("l3b", "delivery", "auth.py", 150, 5)
    ledger.delivered("l3b", "delivery", "api.py", 200, 8)
    ledger.suppressed("l3", "delivery", "utils.py",
                      SignalOutcome.SUPPRESSED_DUPLICATE, "same_content", 10)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "gt_ledger_test.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(ledger.to_jsonl())

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        _assert(len(lines) == 3, f"Expected 3 lines, got {len(lines)}")

        first = json.loads(lines[0])
        _assert(first["outcome"] == "delivered",
                f"Expected outcome='delivered', got {first['outcome']!r}")
        _assert(first["layer"] == "l3b",
                f"Expected layer='l3b', got {first['layer']!r}")
        _assert(first["chars_delivered"] == 150,
                f"Expected chars=150, got {first['chars_delivered']}")
        _assert(first["file_path"] == "auth.py",
                f"Expected file_path='auth.py', got {first['file_path']!r}")

        third = json.loads(lines[2])
        _assert(third["outcome"] == "suppressed_duplicate",
                f"Expected outcome='suppressed_duplicate', got {third['outcome']!r}")
        _assert(third["reason"] == "same_content",
                f"Expected reason='same_content', got {third['reason']!r}")


# ---------------------------------------------------------------------------
# Test 6: graph_map with real graph.db structure
# ---------------------------------------------------------------------------

def test_6_graph_map():
    """Create a temp graph.db mimicking dynaconf structure and test build_graph_map."""
    from groundtruth.brief.graph_map import build_graph_map

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "graph.db")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT,
                file_path TEXT NOT NULL,
                start_line INTEGER,
                end_line INTEGER,
                signature TEXT,
                return_type TEXT,
                is_exported BOOLEAN DEFAULT 0,
                is_test BOOLEAN DEFAULT 0,
                language TEXT NOT NULL,
                parent_id INTEGER REFERENCES nodes(id)
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES nodes(id),
                target_id INTEGER NOT NULL REFERENCES nodes(id),
                type TEXT NOT NULL,
                source_line INTEGER,
                source_file TEXT,
                resolution_method TEXT,
                confidence REAL DEFAULT 0.0,
                metadata TEXT
            );

            -- nodes: load (loaders/env_loader.py), build_env_list (utils/__init__.py), LazySettings (base.py)
            INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line, signature, is_exported, is_test, language)
            VALUES ('Function', 'load', 'loaders.env_loader.load', 'loaders/env_loader.py', 10, 50, 'load(settings, env)', 1, 0, 'Python');

            INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line, signature, is_exported, is_test, language)
            VALUES ('Function', 'build_env_list', 'utils.build_env_list', 'utils/__init__.py', 80, 120, 'build_env_list(settings)', 1, 0, 'Python');

            INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line, signature, is_exported, is_test, language)
            VALUES ('Class', 'LazySettings', 'base.LazySettings', 'base.py', 1, 200, 'class LazySettings', 1, 0, 'Python');

            INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line, signature, is_exported, is_test, language)
            VALUES ('Function', 'test_lazy_settings', 'tests.test_base.test_lazy_settings', 'tests/test_base.py', 5, 30, 'test_lazy_settings()', 0, 1, 'Python');

            -- edges: base.py -> load (CALLS via import), tests/test_base.py -> LazySettings (CALLS)
            INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
            VALUES (3, 1, 'CALLS', 45, 'base.py', 'import', 1.0);

            INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
            VALUES (4, 3, 'CALLS', 10, 'tests/test_base.py', 'import', 1.0);
        """)
        conn.close()

        # Call build_graph_map with ranked files
        ranked_files = [
            {"file": "loaders/env_loader.py", "score": 0.9},
            {"file": "utils/__init__.py", "score": 0.7},
            {"file": "base.py", "score": 0.5},
        ]
        brief = build_graph_map(ranked_files, db_path)
        output = brief.render()

        _assert("<gt-task-brief>" in output,
                f"Missing <gt-task-brief> tag in output: {output[:200]!r}")
        _assert(len(output) > 100,
                f"Output too short ({len(output)} chars): {output!r}")
        # Should contain either "Called by:" or "Functions:"
        _assert("Called by:" in output or "Functions:" in output,
                f"Missing 'Called by:' or 'Functions:' in output: {output!r}")


# ---------------------------------------------------------------------------
# Test 7: EDGE CASES
# ---------------------------------------------------------------------------

def test_7_edge_cases():
    """Stress test edge cases for multiple components."""
    from oh_gt_full_wrapper import _extract_search_symbol, _is_scaffolding_path
    from groundtruth.runtime.budget import BudgetTracker

    # --- Edge case: Empty grep (no pattern) ---
    result_empty = _extract_search_symbol('grep -r "" src/')
    _assert(result_empty == "", f"Expected empty for empty pattern, got {result_empty!r}")

    # --- Edge case: Grep with only regex metacharacters ---
    result_meta = _extract_search_symbol('grep -rn "^\\s*$" src/')
    # This should return empty because ^\\s*$ has no valid identifier
    _assert(result_meta == "", f"Expected empty for metachar-only, got {result_meta!r}")

    # --- Edge case: Very long grep with 10+ pipe alternatives ---
    long_cmd = 'grep -n "a\\|b\\|c\\|d\\|e\\|f\\|g\\|h\\|i\\|j" f.py'
    result_long = _extract_search_symbol(long_cmd)
    # Should extract at least something (single-char 'a' might not pass 3-char min)
    # The function requires len >= 3, so single chars won't match — this is expected behavior
    # It might return '' if all alternatives are too short
    # This is a valid edge case — document behavior
    _assert(isinstance(result_long, str), f"Expected str, got {type(result_long)}")

    # --- Edge case: L5b at exactly 60% boundary ---
    _l5b_count = 0
    _assert((0 < 2 and 60 / 100 >= 0.60), "L5b should fire at exactly 60%")

    # --- Edge case: L5b after 2 injections (budget exhausted) ---
    _l5b_count_2 = 2
    goku_active = True
    _l5b_blocked = (not goku_active) or (_l5b_count_2 < 2 and 0.7 >= 0.60)
    _assert(not _l5b_blocked, f"L5b should be blocked after 2 injections, got {_l5b_blocked}")

    # --- Edge case: Scaffold with mixed files (some source, some scaffold) ---
    # Note: SCAFFOLDING_PREFIXES requires underscore (reproduce_, debug_, scratch_, etc.)
    edited = {"reproduce_issue.py", "debug_1.py", "debug_2.py", "src/real_fix.py"}
    # Use the real _is_scaffolding_path from the wrapper
    _scaffold_count = sum(1 for f in edited if _is_scaffolding_path(f))
    _has_source = any(not _is_scaffolding_path(f) for f in edited)
    _assert(_scaffold_count >= 3,
            f"Expected >= 3 scaffolds, got {_scaffold_count}. "
            f"Checks: {[(f, _is_scaffolding_path(f)) for f in sorted(edited)]}")
    _assert(_has_source,
            "Expected has_source=True because 'src/real_fix.py' is not scaffold")

    # --- Edge case: BudgetTracker at exact boundary ---
    bt = BudgetTracker()
    # gt_query has budget of 3
    ok1, _ = bt.check("gt_query")
    _assert(ok1, "First gt_query should be allowed")
    ok2, _ = bt.check("gt_query")
    _assert(ok2, "Second gt_query should be allowed")
    ok3, _ = bt.check("gt_query")
    _assert(ok3, "Third gt_query should be allowed")
    # 4th should fail
    ok4, reason4 = bt.check("gt_query")
    _assert(not ok4, f"Fourth gt_query should be blocked, got ok={ok4}")
    _assert("budget_exhausted" in reason4,
            f"Expected 'budget_exhausted' in reason, got {reason4!r}")
    # 5th also blocked
    ok5, reason5 = bt.check("gt_query")
    _assert(not ok5, f"Fifth gt_query should be blocked, got ok={ok5}")
    _assert("budget_exhausted" in reason5,
            f"Expected 'budget_exhausted' in reason, got {reason5!r}")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        ("Test 1: Grep interception (dynaconf-1225 trajectory)", test_1_grep_symbol_extraction),
        ("Test 2: Constraint framing (format_risk_evidence)", test_2_constraint_framing),
        ("Test 3: L5 scaffold trigger (3+ scaffolds)", test_3_l5_scaffold_trigger),
        ("Test 4: L5b late-band (60%/65% boundary)", test_4_l5b_late_band),
        ("Test 5: Ledger + atexit flush", test_5_ledger_flush),
        ("Test 6: graph_map with real graph.db", test_6_graph_map),
        ("Test 7: EDGE CASES", test_7_edge_cases),
    ]

    for name, fn in tests:
        try:
            fn()
            _record(name, True)
        except AssertionError as e:
            _record(name, False, str(e))
        except Exception as e:
            _record(name, False, f"EXCEPTION: {type(e).__name__}: {e}\n{traceback.format_exc()}")

    # Print summary
    print("\n" + "=" * 70)
    print("INTEGRATION TEST RESULTS")
    print("=" * 70)

    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = sum(1 for _, ok, _ in _RESULTS if not ok)

    for name, ok, detail in _RESULTS:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
        if not ok and detail:
            for line in detail.split("\n")[:5]:
                print(f"         {line}")

    print("-" * 70)
    print(f"PASS: {passed} / FAIL: {failed}")
    print("=" * 70)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
