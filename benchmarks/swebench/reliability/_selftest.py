"""Self-test: the classifier assigns the right final_class on the 10-task data shapes.

Synthetic contract dicts built from the ACTUAL observed numbers (not gold), to
prove classify.py's decision tree before the live audit run. Run:
  python -m reliability._selftest   (with benchmarks/swebench on PYTHONPATH)
"""
from .classify import classify_task

CASES = {
    # conan: embedder present + discriminates, but semantic scores dropped by the join
    "conan": (dict(
        container={"hard_fail": []}, embedder={"hard_fail": [], "discriminates": True},
        graph={"hard_fail": [], "name_match_dominates": False, "det_pct": 77.77},
        lsp={"hard_fail": [], "lsp_no_op_valid": False, "lsp_did_work": True, "resolved": 27},
        absorption={"absorption_fail": True, "dropped_by_join": 5,
                    "dropped_examples": [{"path": "conans/x.py", "live_join": "MISS"}]},
    ), False, "ABSORPTION_FAIL"),
    # gitingest: 96% deterministic, tiny LSP demand -> valid no-op, gate RED == false fail
    "gitingest": (dict(
        container={"hard_fail": []}, embedder={"hard_fail": [], "discriminates": True},
        graph={"hard_fail": [], "name_match_dominates": False, "det_pct": 96.33,
               "deterministic_count": 105, "name_match_count": 4},
        lsp={"hard_fail": [], "lsp_no_op_valid": True, "lsp_did_work": False,
             "lsp_no_op_reason": "residual=4 on a 96% det graph", "resolved": 0, "residual": 4},
        absorption={"absorption_fail": False, "dropped_by_join": 0},
    ), False, "GATE_FALSE_FAIL"),
    # checkov: 69% det, single in-scope residual -> valid no-op, gate RED == false fail
    "checkov": (dict(
        container={"hard_fail": []}, embedder={"hard_fail": [], "discriminates": True},
        graph={"hard_fail": [], "name_match_dominates": False, "det_pct": 69.37},
        lsp={"hard_fail": [], "lsp_no_op_valid": True, "lsp_did_work": False,
             "resolved": 0, "residual": 1, "lsp_no_op_reason": "residual=1"},
        absorption={"absorption_fail": False, "dropped_by_join": 0},
    ), False, "GATE_FALSE_FAIL"),
    # loguru: name_match dominates the call graph -> genuine product/graph quality
    "loguru": (dict(
        container={"hard_fail": []}, embedder={"hard_fail": [], "discriminates": True},
        graph={"hard_fail": [], "name_match_dominates": True, "det_pct": 36.65,
               "deterministic_count": 718, "name_match_count": 1241},
        lsp={"hard_fail": [], "lsp_no_op_valid": False, "lsp_did_work": True, "resolved": 20},
        absorption={"absorption_fail": False, "dropped_by_join": 0},
    ), False, "PRODUCT_QUALITY_FAIL"),
    # cfn-lint: green, both surfaces robust
    "cfn-lint": (dict(
        container={"hard_fail": []}, embedder={"hard_fail": [], "discriminates": True},
        graph={"hard_fail": [], "name_match_dominates": False, "det_pct": 83.0},
        lsp={"hard_fail": [], "lsp_no_op_valid": False, "lsp_did_work": True, "resolved": 73},
        absorption={"absorption_fail": False, "dropped_by_join": 0},
        gate_metrics={"semantic_signal_count": 4},
    ), True, "GREEN_ROBUST"),
    # aiogram: green but thin (1 semantic signal)
    "aiogram": (dict(
        container={"hard_fail": []}, embedder={"hard_fail": [], "discriminates": True},
        graph={"hard_fail": [], "name_match_dominates": False, "det_pct": 70.0},
        lsp={"hard_fail": [], "lsp_no_op_valid": False, "lsp_did_work": True, "resolved": 63},
        absorption={"absorption_fail": False, "dropped_by_join": 0},
        gate_metrics={"semantic_signal_count": 1},
    ), True, "GREEN_THIN"),
    # a container-broken task must be caught BEFORE any quality verdict
    "broken-container": (dict(
        container={"hard_fail": ["required_flags_not_all_set"]},
        embedder={"hard_fail": []}, graph={"hard_fail": []}, lsp={"hard_fail": []},
        absorption={"absorption_fail": False},
    ), False, "CONTAINER_RUNTIME_FAIL"),
}


def main() -> int:
    ok = True
    for name, (c, gate_passed, expected) in CASES.items():
        got = classify_task(c, gate_passed)["final_class"]
        mark = "ok" if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"  {mark:4} {name:18} expected={expected:22} got={got}")
    print("SELFTEST_OK" if ok else "SELFTEST_FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
