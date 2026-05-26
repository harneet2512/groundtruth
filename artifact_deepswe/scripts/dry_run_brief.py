#!/usr/bin/env python3
"""Dry run: test brief generation on existing graph.db files.

Since gt-index can't run on this Windows machine (DLL dependency), we test
the brief generation pipeline using holdout graph.db files that match
DeepSWE languages.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

# Add GT source to path
GT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GT_ROOT))

HOLDOUT_DIR = GT_ROOT / ".tmp_holdout" / "bugs"

# Test cases: (instance_id, graph_db, repo_root, language, fake_issue_text)
TEST_CASES = [
    {
        "instance_id": "hono-4876",
        "language": "TypeScript",
        "graph_db": str(HOLDOUT_DIR / "hono-4876" / "graph.db"),
        "repo_root": str(HOLDOUT_DIR / "hono-4876"),
        "issue_text": "The Context.header() method in Hono does not properly handle multiple Set-Cookie headers. When setting multiple cookies, only the last one is preserved. Fix the header handling to support append mode for Set-Cookie headers.",
    },
    {
        "instance_id": "crossplane-7332",
        "language": "Go",
        "graph_db": str(HOLDOUT_DIR / "crossplane-7332" / "graph.db"),
        "repo_root": str(HOLDOUT_DIR / "crossplane-7332"),
        "issue_text": "Crossplane provider revision controller does not reconcile when a provider config reference changes. The watch should include field selectors for the provider config ref. Add proper event handling for config ref changes in the revision reconciler.",
    },
    {
        "instance_id": "marimo-9408",
        "language": "Python",
        "graph_db": str(HOLDOUT_DIR / "marimo-9408" / "graph.db"),
        "repo_root": str(HOLDOUT_DIR / "marimo-9408"),
        "issue_text": "The marimo notebook cell output is not properly handling HTML content with embedded JavaScript. When a cell returns HTML with script tags, the scripts are not executed in the output iframe. Fix the output rendering to properly handle embedded scripts.",
    },
]


def run_test(case: dict) -> dict:
    """Test brief generation for a single case."""
    result = {
        "instance_id": case["instance_id"],
        "language": case["language"],
        "graph_db_exists": os.path.exists(case["graph_db"]),
        "repo_root_exists": os.path.exists(case["repo_root"]),
    }

    if not result["graph_db_exists"]:
        result["error"] = "graph.db not found"
        return result

    # Test v22 brief
    try:
        from groundtruth.pretask.v22_brief import generate_brief
        v22_result = generate_brief(
            issue_text=case["issue_text"],
            repo_path=case["repo_root"],
            graph_db_path=case["graph_db"],
        )
        result["v22_brief"] = v22_result or "(empty)"
        result["v22_length"] = len(v22_result) if v22_result else 0
        result["v22_success"] = bool(v22_result)
    except Exception as e:
        result["v22_error"] = f"{type(e).__name__}: {e}"
        result["v22_success"] = False

    # Test v1r brief
    try:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief
        v1r_result = generate_v1r_brief(
            issue_text=case["issue_text"],
            repo_root=case["repo_root"],
            graph_db=case["graph_db"],
            bug_id=case["instance_id"],
        )
        if v1r_result and v1r_result.brief_text:
            result["v1r_brief"] = v1r_result.brief_text
            result["v1r_length"] = len(v1r_result.brief_text)
            result["v1r_files"] = len(v1r_result.files)
            result["v1r_token_estimate"] = v1r_result.token_estimate
            result["v1r_success"] = True
        else:
            result["v1r_brief"] = "(empty)"
            result["v1r_length"] = 0
            result["v1r_success"] = False
    except Exception as e:
        result["v1r_error"] = f"{type(e).__name__}: {e}"
        result["v1r_success"] = False

    # Test anchor extraction
    try:
        from groundtruth.pretask.anchors import extract_issue_anchors
        anchors = extract_issue_anchors(case["issue_text"], case["graph_db"])
        result["anchors"] = {
            "symbols": list(getattr(anchors, "symbols", []))[:10],
            "paths": list(getattr(anchors, "paths", []))[:5],
            "test_names": list(getattr(anchors, "test_names", []))[:5],
        }
        result["anchors_success"] = True
    except Exception as e:
        result["anchors_error"] = f"{type(e).__name__}: {e}"
        result["anchors_success"] = False

    # Test inject.py wrapper
    try:
        sys.path.insert(0, str(GT_ROOT / "artifact_deepswe" / "gt_integration"))
        from inject import generate_deepswe_brief
        inject_result = generate_deepswe_brief(
            instance_id=case["instance_id"],
            problem_statement=case["issue_text"],
            graph_db=case["graph_db"],
            repo_root=case["repo_root"],
        )
        result["inject_brief"] = inject_result[:200] + "..." if len(inject_result) > 200 else inject_result
        result["inject_length"] = len(inject_result)
        result["inject_success"] = bool(inject_result)
    except Exception as e:
        result["inject_error"] = f"{type(e).__name__}: {e}"
        result["inject_success"] = False

    return result


def main():
    # Force UTF-8 output on Windows
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    print("=" * 70)
    print("DeepSWE + GroundTruth Dry Run -- Brief Generation Test")
    print("=" * 70)

    results = []
    for case in TEST_CASES:
        print(f"\n{'-' * 60}")
        print(f"  {case['instance_id']} ({case['language']})")
        print(f"  graph.db: {case['graph_db']}")
        print(f"{'-' * 60}")

        result = run_test(case)
        results.append(result)

        # Print summary
        print(f"  graph.db exists: {result.get('graph_db_exists')}")
        print(f"  v22 brief: {'OK' if result.get('v22_success') else 'FAIL'} ({result.get('v22_length', 0)} chars)")
        print(f"  v1r brief: {'OK' if result.get('v1r_success') else 'FAIL'} ({result.get('v1r_length', 0)} chars, {result.get('v1r_files', 0)} files)")
        print(f"  anchors:   {'OK' if result.get('anchors_success') else 'FAIL'}")
        print(f"  inject.py: {'OK' if result.get('inject_success') else 'FAIL'} ({result.get('inject_length', 0)} chars)")

        if result.get("v22_error"):
            print(f"  v22 error: {result['v22_error']}")
        if result.get("v1r_error"):
            print(f"  v1r error: {result['v1r_error']}")
        if result.get("anchors_error"):
            print(f"  anchors error: {result['anchors_error']}")
        if result.get("inject_error"):
            print(f"  inject error: {result['inject_error']}")

        # Show brief preview
        if result.get("inject_success"):
            print(f"\n  Brief preview:")
            for line in (result.get("inject_brief") or "").split("\n")[:8]:
                print(f"    {line}")

    # Summary
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    for r in results:
        v22 = "Y" if r.get("v22_success") else "N"
        v1r = "Y" if r.get("v1r_success") else "N"
        anc = "Y" if r.get("anchors_success") else "N"
        inj = "Y" if r.get("inject_success") else "N"
        print(f"  {r['instance_id']:30s} {r['language']:12s} v22={v22} v1r={v1r} anchors={anc} inject={inj}")

    # Write results
    out_path = GT_ROOT / "artifact_deepswe" / "dry_run_results.json"
    # Remove full brief text from JSON to keep it readable
    json_results = []
    for r in results:
        jr = {k: v for k, v in r.items() if not k.endswith("_brief")}
        json_results.append(jr)
    with open(out_path, "w") as f:
        json.dump(json_results, f, indent=2)
    print(f"\nDetailed results: {out_path}")


if __name__ == "__main__":
    main()
