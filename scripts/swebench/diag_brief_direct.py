#!/usr/bin/env python3
"""Direct test: does _generate_brief_for return non-empty content
for the kozea__weasyprint-2300 instance?

This isolates causes (b)/(c)/(d) without firing the full probe.
"""
import os
import sys


def main():
    iid = "kozea__weasyprint-2300"

    # Build a minimal instance with attribute access (mimics pd.Series.attr)
    class FakeInstance:
        instance_id = iid
        # Pull problem_statement from the dataset's cached file
        problem_statement = ""

    # Load real problem_statement from the dataset
    try:
        from datasets import load_dataset
        ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")
        rows = [r for r in ds if r["instance_id"] == iid]
        if rows:
            FakeInstance.problem_statement = rows[0]["problem_statement"]
            print(f"loaded real problem_statement: {len(FakeInstance.problem_statement)} chars")
        else:
            print("WARN: instance not found in dataset; using stub")
            FakeInstance.problem_statement = "make flex layout work with overflow auto"
    except Exception as exc:
        print(f"dataset load failed: {exc}")
        FakeInstance.problem_statement = "make flex layout work with overflow auto"

    # Now invoke the wrapper's _generate_brief_for
    sys.path.insert(0, "/home/ubuntu/Groundtruth/scripts/swebench")
    sys.path.insert(0, "/home/ubuntu/Groundtruth")
    sys.path.insert(0, "/home/ubuntu/Groundtruth/src")

    os.environ.setdefault("GT_PREBUILT_INDEXES_ROOT", "/home/ubuntu/eval_indexes")
    os.environ.setdefault("GT_REPO_EXTRACTS_ROOT", "/home/ubuntu/eval_repos")
    os.environ.setdefault("GT_V22_TIER1", "1")
    os.environ.setdefault("GT_V22_MULTIHOP", "2")
    os.environ.setdefault("GT_V22_GLOBAL_BM25", "1")

    from oh_gt_live_lite_v2_wrapper import _generate_brief_for

    print(f"\n=== calling _generate_brief_for on {iid} ===")
    brief = _generate_brief_for(FakeInstance())
    print(f"\n=== RESULT: brief len={len(brief)} ===")
    if brief:
        print(brief[:2000])
    else:
        print("(brief is EMPTY)")


if __name__ == "__main__":
    main()
