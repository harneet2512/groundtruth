#!/usr/bin/env python3
"""Local smoke test for gt_hook.py and analyze_hook_logs.py.

Verifies:
  1. gt_hook.py runs without crash (exit 0)
  2. All 13 evidence classes load, SiblingAnalyzer works, formatting works,
     abstention works, JSONL log writes
  3. analyze_hook_logs.py parses v4 log format and passes smoke gate
  4. analyze_hook_logs.py --json produces valid stats

Usage:
    python tests/smoke_gt_hook.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GT_HOOK = os.path.join(REPO, "benchmarks", "swebench", "gt_hook.py")
ANALYZER = os.path.join(REPO, "scripts", "swebench", "analyze_hook_logs.py")


def test_basic_run():
    """gt_hook.py runs without crash."""
    r = subprocess.run(
        [sys.executable, GT_HOOK, "--root=.", "--db=/tmp/gt_test.db", "--quiet", "--max-items=3"],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=REPO,
    )
    print(f"Test 1 -- basic run: exit={r.returncode}")
    if r.stdout.strip():
        print(f"  Output: {r.stdout.strip()[:200]}")
    else:
        print("  Output: (empty)")
    assert r.returncode == 0, f"Non-zero exit: {r.stderr[:300]}"
    print("  PASS")


def test_unit_tests():
    """All evidence classes load, SiblingAnalyzer works, formatting, abstention, logging."""
    # Register a module name so dataclasses work
    import types

    mod = types.ModuleType("gt_hook")
    sys.modules["gt_hook"] = mod

    with open(GT_HOOK) as fh:
        code = fh.read()
    ns: dict = {"__name__": "gt_hook", "__file__": GT_HOOK}
    exec(compile(code, GT_HOOK, "exec"), ns)

    # 13 classes
    classes = [
        "ChangeEvidence",
        "CallerExpectation",
        "TestExpectation",
        "PatternEvidence",
        "StructuralEvidence",
        "SemanticEvidence",
        "ChangeAnalyzer",
        "CallerUsageMiner",
        "TestAssertionMiner",
        "SiblingAnalyzer",
        "CallSiteVoter",
        "ArgumentAffinityChecker",
        "GuardConsistencyChecker",
    ]
    for c in classes:
        assert c in ns, f"Missing: {c}"
    print("Test 2 -- unit tests:")
    print("  13/13 evidence classes present")

    # SiblingAnalyzer
    source = "\n".join(
        [
            "class Proc:",
            "    def handle_a(self, req):",
            '        if not req: raise ValueError("bad")',
            "        return req.data",
            "    def handle_b(self, req):",
            '        if not req: raise ValueError("bad")',
            "        return req.data",
            "    def handle_c(self, req):",
            '        if not req: raise ValueError("bad")',
            "        return req.data",
            "    def handle_new(self, req):",
            "        return req.data",
        ]
    )
    findings = ns["SiblingAnalyzer"]().analyze(source, "handle_new", file_path="test.py")
    print(f"  SiblingAnalyzer: {len(findings)} findings")
    for f in findings:
        print(f"    {f.kind}: {f.message[:70]}")
    assert len(findings) >= 1, "Should detect missing guard"

    # Format evidence
    items = [
        ns["ChangeEvidence"](
            kind="guard_removed", file_path="t.py", line=10, message="removed", confidence=0.8
        ),
        ns["PatternEvidence"](
            kind="missing_guard",
            file_path="t.py",
            line=5,
            message="3/4 have guards",
            confidence=0.75,
        ),
        ns["SemanticEvidence"](
            kind="csv", file_path="t.py", line=20, message="5/7 pass user", confidence=0.85
        ),
        ns["CallerExpectation"](
            file_path="t.py",
            line=30,
            usage_type="destructure_tuple",
            detail="3 callers destructure",
            confidence=0.9,
        ),
        ns["TestExpectation"](
            test_file="test.py",
            test_func="test_ser",
            line=42,
            assertion_type="assertEqual",
            expected="fmt",
            confidence=0.85,
        ),
    ]
    for item in items:
        fmt = ns["_format_evidence"](item)
        # New format: [TIER] IMPERATIVE_MSG (confidence)
        assert fmt.startswith("[") and "(" in fmt, f"Bad format: {fmt}"
    print("  All 5 evidence types format correctly")

    # Abstention
    test_items = [
        ns["ChangeEvidence"](kind="a", file_path="x", line=1, message="low", confidence=0.3),
        ns["ChangeEvidence"](kind="b", file_path="x", line=2, message="high", confidence=0.8),
        ns["ChangeEvidence"](kind="c", file_path="x", line=3, message="_private", confidence=0.9),
    ]
    passed = ns["_apply_abstention"](test_items, min_confidence=0.65)
    assert len(passed) == 1 and passed[0].kind == "b"
    print("  Abstention: 3 -> 1 (correct)")

    # JSONL log
    log_path = ns["HOOK_LOG"]
    if os.path.exists(log_path):
        os.remove(log_path)
    ns["log_hook"]({"test": True, "hook": "smoke"})
    with open(log_path) as fh:
        entry = json.loads(fh.readline())
    assert entry["test"] is True and "timestamp" in entry
    print("  JSONL log OK")
    print("  PASS")


def _make_fake_logs(tmpdir: str) -> str:
    fake_log = os.path.join(tmpdir, "django__django-10097.jsonl")
    entries = [
        {
            "hook": "post_edit",
            "endpoint": "verify",
            "root": "/testbed",
            "files_changed": ["django/db/models/query.py"],
            "evidence": {
                "change": {"ran": True, "items_found": 1, "after_abstention": 1},
                "contract": {
                    "ran": True,
                    "callers_analyzed": 3,
                    "tests_analyzed": 1,
                    "items_found": 2,
                    "after_abstention": 1,
                },
                "pattern": {"ran": True, "items_found": 0, "after_abstention": 0},
                "structural": {"ran": True, "items_found": 0, "after_abstention": 0},
                "semantic": {"ran": True, "items_found": 1, "after_abstention": 1},
            },
            "abstention_summary": {"total_raw": 4, "total_emitted": 3, "total_suppressed": 1},
            "output": "GT: safety check removed [change]\nGT: 3 callers destructure [contract]",
            "output_lines": 2,
            "wall_time_ms": 342,
        },
        {
            "hook": "post_edit",
            "endpoint": "verify",
            "root": "/testbed",
            "files_changed": [],
            "evidence": {},
            "abstention_summary": {"total_raw": 0, "total_emitted": 0, "total_suppressed": 0},
            "output": "",
            "output_lines": 0,
            "wall_time_ms": 5,
        },
    ]
    with open(fake_log, "w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return tmpdir


def test_analyzer_smoke_gate():
    """analyze_hook_logs.py parses v4 format and smoke gate passes."""
    tmpdir = tempfile.mkdtemp(prefix="gt_smoke_")
    try:
        _make_fake_logs(tmpdir)
        r = subprocess.run(
            [sys.executable, ANALYZER, tmpdir, "--smoke-gate", "1"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        print("Test 3 -- analyze_hook_logs.py (smoke gate):")
        for line in r.stdout.strip().splitlines():
            print(f"  {line}")
        if r.returncode != 0:
            print(f"  STDERR: {r.stderr[:300]}")
        assert r.returncode == 0, f"Analyzer failed: {r.stderr[:300]}"
        assert "PASS" in r.stdout, "Smoke gate should pass"
        print("  PASS")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_analyzer_json():
    """analyze_hook_logs.py --json produces valid stats."""
    tmpdir = tempfile.mkdtemp(prefix="gt_smoke2_")
    try:
        _make_fake_logs(tmpdir)
        r = subprocess.run(
            [sys.executable, ANALYZER, tmpdir, "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0, f"JSON output failed: {r.stderr[:200]}"
        stats = json.loads(r.stdout)
        assert stats["total_invocations"] == 2
        assert stats["fired"] == 1
        assert stats["tasks_with_fire"] == 1
        pct = stats["emission_rate"] * 100
        print(
            f"Test 4 -- JSON output: invocations={stats['total_invocations']}, "
            f"fired={stats['fired']}, emission_rate={pct:.0f}%"
        )
        print("  PASS")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    print("=" * 60)
    print("  LOCAL SMOKE TESTS FOR gt_hook.py")
    print("=" * 60)
    print()

    test_basic_run()
    print()
    test_unit_tests()
    print()
    test_analyzer_smoke_gate()
    print()
    test_analyzer_json()
    print()

    print("=" * 60)
    print("  ALL 4 LOCAL SMOKE TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
