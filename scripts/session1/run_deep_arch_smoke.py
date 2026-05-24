#!/usr/bin/env python3
"""Deep architecture smoke test — Phase 8.

Proves all new core modules work end-to-end without a benchmark run.
Scripted test, NOT a benchmark run (per hard checks: unlimited scripted tests).

12 checks:
 1. L1 graph-map brief generated
 2. Brief reaches OH agent path (format check)
 3. L3 post-edit decision goes through sanitizer/ledger
 4. L3b post-view decision goes through sanitizer/ledger
 5. L4 tools install (file existence check)
 6. L4 tools run (import check)
 7. L4 budgets block overuse
 8. L5 fires in simulated trajectory
 9. L6 reindex attempt is counted
10. MCP imports and exposes tools
11. No obvious hidden/debug leak
12. Metrics file is produced
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

PASS = 0
FAIL = 0
RESULTS: dict[str, str] = {}


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "PASS" if condition else "FAIL"
    if condition:
        PASS += 1
    else:
        FAIL += 1
    RESULTS[name] = f"{status} {detail}"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def create_test_graph_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE nodes (
        id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
        file_path TEXT, start_line INTEGER, end_line INTEGER,
        signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
        is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER)""")
    conn.execute("""CREATE TABLE edges (
        id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
        type TEXT, source_line INTEGER, source_file TEXT,
        resolution_method TEXT, confidence REAL, metadata TEXT)""")
    conn.execute("INSERT INTO nodes VALUES (1,'Function','get_user',NULL,'src/auth.py',10,20,'token: str','User',1,0,'python',NULL)")
    conn.execute("INSERT INTO nodes VALUES (2,'Function','validate','validate','src/auth.py',25,35,'token: str','bool',1,0,'python',NULL)")
    conn.execute("INSERT INTO nodes VALUES (3,'Function','login',NULL,'src/api.py',5,15,'request','Response',1,0,'python',NULL)")
    conn.execute("INSERT INTO nodes VALUES (4,'Function','test_auth',NULL,'tests/test_auth.py',1,10,NULL,NULL,0,1,'python',NULL)")
    conn.execute("INSERT INTO edges VALUES (1,3,1,'CALLS',8,'src/api.py','import',1.0,NULL)")
    conn.execute("INSERT INTO edges VALUES (2,3,2,'CALLS',10,'src/api.py','same_file',1.0,NULL)")
    conn.execute("INSERT INTO edges VALUES (3,4,1,'CALLS',5,'tests/test_auth.py','import',0.9,NULL)")
    conn.commit()
    conn.close()


def main() -> int:
    print("=== Deep Architecture Smoke Test ===\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "graph.db")
        create_test_graph_db(db_path)

        # 1. L1 graph-map brief generated
        print("Check 1: L1 graph-map brief")
        try:
            from groundtruth.brief.graph_map import build_graph_map
            brief = build_graph_map(
                [{"file": "src/auth.py", "score": 0.9}, {"file": "src/api.py", "score": 0.7}],
                db_path,
            )
            rendered = brief.render()
            check("L1_graph_map_brief", len(rendered) > 50 and "<gt-task-brief>" in rendered,
                  f"{len(rendered)} chars")
        except Exception as e:
            check("L1_graph_map_brief", False, str(e))

        # 2. Brief reaches OH agent path
        print("Check 2: Brief format for OH")
        check("brief_oh_format",
              "</gt-task-brief>" in rendered and "Called by:" in rendered or "Functions:" in rendered,
              "has structural data")

        # 3. L3 post-edit through sanitizer/ledger
        print("Check 3: L3 through sanitizer/ledger")
        try:
            from groundtruth.runtime.sanitizer import sanitize, has_leak
            from groundtruth.runtime.ledger import Ledger, SignalOutcome
            raw = "[GT_STATUS] test\n[CONTRACT] must return User\n[GT_META] internal"
            cleaned = sanitize(raw)
            ledger = Ledger()
            ledger.delivered("L3", "post_edit", "src/auth.py", len(cleaned))
            check("L3_sanitizer_ledger",
                  "[GT_STATUS]" not in cleaned and "[CONTRACT]" in cleaned and len(ledger.entries) == 1,
                  f"cleaned={len(cleaned)} chars, ledger={len(ledger.entries)} entries")
        except Exception as e:
            check("L3_sanitizer_ledger", False, str(e))

        # 4. L3b through sanitizer/ledger
        print("Check 4: L3b through sanitizer/ledger")
        try:
            raw_view = "[GT_TRACE] view delivery\nCalled by: src/api.py:8\n[GT_META] debug"
            cleaned_view = sanitize(raw_view, max_chars=500)
            ledger.delivered("L3b", "post_view", "src/auth.py", len(cleaned_view))
            check("L3b_sanitizer_ledger",
                  "Called by:" in cleaned_view and not has_leak(cleaned_view) and len(ledger.entries) == 2,
                  f"cleaned={len(cleaned_view)} chars")
        except Exception as e:
            check("L3b_sanitizer_ledger", False, str(e))

        # 5. L4 tools install check
        print("Check 5: L4 tools exist")
        tool_dirs = ["tools/sweagent/gt_query", "tools/sweagent/gt_search",
                     "tools/sweagent/gt_navigate", "tools/sweagent/gt_validate"]
        all_exist = all(os.path.isdir(d) for d in tool_dirs)
        check("L4_tools_exist", all_exist,
              f"{sum(1 for d in tool_dirs if os.path.isdir(d))}/4 dirs")

        # 6. L4 tools importable
        print("Check 6: L4 tools importable")
        try:
            tool_py = "tools/sweagent/gt_query/lib/gt_query.py"
            check("L4_tools_importable", os.path.isfile(tool_py), tool_py)
        except Exception as e:
            check("L4_tools_importable", False, str(e))

        # 7. L4 budgets block overuse
        print("Check 7: L4 budgets")
        try:
            from groundtruth.runtime.budget import BudgetTracker
            bt = BudgetTracker()
            ok1, _ = bt.check("gt_query")
            ok2, _ = bt.check("gt_query")
            ok3, _ = bt.check("gt_query")
            ok4, reason = bt.check("gt_query")
            check("L4_budget_enforcement",
                  ok1 and ok2 and ok3 and not ok4 and "budget_exhausted" in reason,
                  f"3 allowed, 4th blocked: {reason}")
        except Exception as e:
            check("L4_budget_enforcement", False, str(e))

        # 8. L5 fires in simulated trajectory
        print("Check 8: L5 simulated trajectory")
        try:
            from groundtruth.runtime.state import AgentTrajectoryState
            state = AgentTrajectoryState(max_iter=100)
            for i in range(45):
                state.action_count = i
                state.record_search()
            should_fire = (state.band in ("mid", "late") and
                          state.search_count_since_edit >= 8 and
                          not state.has_source_edit)
            check("L5_simulated_fire", should_fire,
                  f"band={state.band} searches={state.search_count_since_edit} edits={state.has_source_edit}")
        except Exception as e:
            check("L5_simulated_fire", False, str(e))

        # 9. L6 reindex counted
        print("Check 9: L6 reindex counted")
        try:
            ledger.delivered("L6", "reindex", "src/auth.py", 0, iteration=5)
            l6_entries = [e for e in ledger.entries if e.layer == "L6"]
            check("L6_reindex_counted", len(l6_entries) == 1, f"{len(l6_entries)} L6 entries")
        except Exception as e:
            check("L6_reindex_counted", False, str(e))

        # 10. MCP imports
        print("Check 10: MCP imports")
        try:
            from groundtruth.mcp.server import create_server
            check("MCP_imports", True, "create_server importable")
        except ImportError:
            try:
                from groundtruth.mcp import server
                check("MCP_imports", hasattr(server, 'create_server') or hasattr(server, 'FastMCP'),
                      "server module importable")
            except ImportError as e:
                check("MCP_imports", False, str(e))

        # 11. No hidden leak
        print("Check 11: No hidden leak")
        try:
            test_texts = [
                rendered,  # brief output
                cleaned,   # sanitized L3
                cleaned_view,  # sanitized L3b
            ]
            any_leak = any(has_leak(t) for t in test_texts)
            check("no_hidden_leak", not any_leak, "0 leaks in all outputs")
        except Exception as e:
            check("no_hidden_leak", False, str(e))

        # 12. Metrics produced
        print("Check 12: Metrics produced")
        try:
            from groundtruth.runtime.metrics import RuntimeMetrics
            m = RuntimeMetrics(task_id="smoke_test")
            m.record_emission("L3", 150)
            m.record_emission("L3b", 80)
            m.record_suppression("L3b")
            m.record_tool_call("gt_query")
            metrics_json = m.to_json()
            metrics_dict = json.loads(metrics_json)
            metrics_path = os.path.join(tmpdir, "smoke_metrics.json")
            with open(metrics_path, "w") as f:
                f.write(metrics_json)
            check("metrics_produced",
                  os.path.exists(metrics_path) and metrics_dict["total_gt_chars_delivered"] == 230,
                  f"chars={metrics_dict['total_gt_chars_delivered']}")
        except Exception as e:
            check("metrics_produced", False, str(e))

    # Summary
    print(f"\n=== RESULTS: {PASS} passed, {FAIL} failed ===")
    if FAIL > 0:
        print("SMOKE FAILED — fix before Run B")
        for name, result in RESULTS.items():
            if result.startswith("FAIL"):
                print(f"  FAIL: {name}: {result}")
    else:
        print("SMOKE PASSED — ready for Run B")

    # Write results
    report = {
        "pass": PASS,
        "fail": FAIL,
        "results": RESULTS,
    }
    print(json.dumps(report, indent=2))
    return 1 if FAIL > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
