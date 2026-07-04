"""Harness verification (STEP 1) — prove the contracts catch what they must.

Covers: absorption positive+negative, run_contract missing/mismatched gt_commit
+ task_ids-ignored, container_contract wrong-import-path. Pure synthetic inputs.
Run:  python -m reliability._selftest_harness   (benchmarks/swebench on PYTHONPATH)
"""
from __future__ import annotations

import json
import os
import tempfile

from .absorption_contract import build_absorption_contract
from .container_contract import build_container_contract
from .run_contract import build_run_contract


def _dump(d, name, obj):
    json.dump(obj, open(os.path.join(d, name), "w", encoding="utf-8"))


def t_absorption() -> str:
    # POSITIVE: a semantic score existed upstream (consistent>0) but the live
    # exact-path join lost it (live==0) -> ABSORPTION_FAIL. (the conan shape)
    d = tempfile.mkdtemp()
    _dump(d, "10_candidates_rendered.json", [
        {"candidate_id": "a.py:1:f", "path": "a.py", "live_join": "MISS",
         "live_sem": 0.0, "consistent_sem": 0.5, "routes": ["graph_witness"]},
        {"candidate_id": "b.py:1:g", "path": "b.py", "live_join": "MATCH",
         "live_sem": 0.8, "consistent_sem": 0.8, "routes": ["semantic"]},
    ])
    _dump(d, "11_gate_metrics.json", {"semantic_signal_count": 1})
    pos = build_absorption_contract(d)
    assert pos["absorption_fail"] is True and pos["dropped_by_join"] == 1, pos
    assert pos["gate_matches_live"] is True, pos  # gate sem_count 1 == live_pos 1

    # NEGATIVE: every rendered entry's live sem == consistent (nothing dropped)
    d2 = tempfile.mkdtemp()
    _dump(d2, "10_candidates_rendered.json", [
        {"candidate_id": "a.py:1:f", "path": "a.py", "live_join": "MATCH",
         "live_sem": 0.7, "consistent_sem": 0.7, "routes": ["semantic"]},
        {"candidate_id": "b.py:1:g", "path": "b.py", "live_join": "MATCH",
         "live_sem": 0.0, "consistent_sem": 0.0, "routes": ["graph"]},
    ])
    neg = build_absorption_contract(d2)
    assert neg["absorption_fail"] is False and neg["dropped_by_join"] == 0, neg
    return f"absorption pos(dropped={pos['dropped_by_join']}) + neg(dropped={neg['dropped_by_join']})"


def t_run_contract() -> str:
    missing = build_run_contract({"resolved_gt_sha": "abc", "gt_use_substrate_image": "true"})
    assert "gt_commit_input_missing" in missing["hard_fail"], missing

    mismatch = build_run_contract({"gt_commit_input": "gt-trial", "resolved_gt_sha": "abc",
                                   "expected_gt_sha": "def", "gt_use_substrate_image": "true"})
    assert "resolved_sha_mismatch" in mismatch["hard_fail"], mismatch

    no_sha = build_run_contract({"gt_commit_input": "gt-trial", "gt_use_substrate_image": "true"})
    assert "resolved_sha_missing" in no_sha["hard_fail"], no_sha

    ti_ignored = build_run_contract({"gt_commit_input": "x", "resolved_gt_sha": "abc",
                                     "gt_use_substrate_image": "true",
                                     "task_ids_parsed": ["t1", "t2"], "matrix_tasks": ["t3"]})
    assert "task_ids_ignored_or_replaced_by_slicing" in ti_ignored["hard_fail"], ti_ignored

    clean = build_run_contract({"gt_commit_input": "gt-trial", "resolved_gt_sha": "abc",
                                "gt_use_substrate_image": "true",
                                "task_ids_parsed": ["t1"], "matrix_tasks": ["t1"]})
    assert not clean["hard_fail"], clean
    return "run_contract catches: missing/mismatch/no-sha/task_ids-ignored; clean=ok"


def t_container_contract() -> str:
    # Locally, groundtruth imports from the checkout (NOT /opt/gt), so the contract
    # MUST flag it — exactly the "GT importing from the wrong path" case.
    c = build_container_contract("t", "/nonexistent", "/tmp/does/not/exist/graph.db")
    assert "import_from_opt_gt" in c and "hard_fail" in c, c
    if not c.get("import_from_opt_gt"):
        assert "groundtruth_imported_from_non_opt_gt" in c["hard_fail"], c
    assert "graph_not_built_in_container" in c["hard_fail"], c  # bogus graph path caught
    return f"container_contract flags wrong import path (import_from_opt_gt={c.get('import_from_opt_gt')})"


def main() -> int:
    ok = True
    for fn in (t_absorption, t_run_contract, t_container_contract):
        try:
            print(f"  ok   {fn.__name__:18} {fn()}")
        except AssertionError as e:
            ok = False
            print(f"  FAIL {fn.__name__:18} {e}")
    print("HARNESS_SELFTEST_OK" if ok else "HARNESS_SELFTEST_FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
