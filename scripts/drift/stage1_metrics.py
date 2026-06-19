"""Stage-1 metrics for contract-DELTA: DETERMINISM + CORRECTNESS, on controlled inputs.

Per CLAUDE.md two-stage methodology: Stage 1 = GT is deterministic + delivers the RIGHT
context, proven by CONTROLLED deterministic verification (NOT flips). "DEFINITION OF DONE:
metrics changed." This emits the numbers, 8-dp:

  false_positive_rate   — delta lines that are NOT real contract changes / total delta lines
  true_positive_capture — real contract changes the delta caught / real changes present
  determinism_rate      — scenarios whose output is byte-identical across N runs / total

Headline: the arviz run4 failure (17 false positives from restructuring churn) must now be 0.
Real gt-index binary; same-path before/after index; no agent, no live run — fully deterministic.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

from groundtruth.hooks.contract_delta import compute_delta

BIN = os.environ.get("GT_INDEX_BINARY") or r"D:\Groundtruth\gt-index\gt-index-current.exe"
os.environ["GT_INDEX_BINARY"] = BIN
N_RUNS = 5  # determinism: each scenario run N times, assert identical

# Each scenario: (name, old_src, new_src, expected_substrings, forbidden_substrings)
# expected = the REAL contract change the delta MUST report (true positives).
# forbidden = anything that would be a FALSE positive (pre-existing/restructuring churn).
_PLOT_HDI_OLD = (
    "def plot_hdi(x, y=None, hdi_data=None, smooth=True):\n"
    "    if y is None and hdi_data is None:\n"
    "        raise ValueError('one of y/hdi_data required')\n"
    "    if len(x) != 1:\n"
    "        raise ValueError('bad shape')\n"
    "    result = smoothify(x)\n"
    "    return result\n\ndef caller():\n    return plot_hdi([1])\n"
)
SCENARIOS = [
    # 1. THE ARVIZ FAILURE: real TypeError add + `if smooth:` restructuring.
    ("arviz_restructure", _PLOT_HDI_OLD,
     _PLOT_HDI_OLD.replace("    if len(x) != 1:",
                           "    if isinstance(x[0], str):\n        raise TypeError('cat x')\n    if len(x) != 1:")
                  .replace("    result = smoothify(x)", "    if smooth:\n        result = smoothify(x)"),
     ["new raise: TypeError"], ["ValueError", "dropped", "smoothify", "boundary", "len(x)"]),
    # 2. dropped raise (removed a precondition that raises ValueError)
    ("dropped_raise",
     "def f(x):\n    if x is None:\n        raise ValueError('a')\n    return [x]\ndef c():\n    return f(1)\n",
     "def f(x):\n    return [x]\ndef c():\n    return f(1)\n",
     ["dropped raise: ValueError"], ["new raise", "TypeError"]),
    # 3. return shape change
    ("return_change",
     "def g(x):\n    return [x]\ndef c():\n    return g(1)\n",
     "def g(x):\n    return None\ndef c():\n    return g(1)\n",
     ["return shape"], ["raise"]),
    # 4. no-op: identical -> empty (correct-or-quiet)
    ("noop",
     "def h(x):\n    return x\ndef c():\n    return h(1)\n",
     "def h(x):\n    return x\ndef c():\n    return h(1)\n",
     [], ["raise", "return shape", "CONTRACT-DELTA"]),
    # 5. non-contract edit (rename a local) -> no contract change
    ("non_contract_edit",
     "def k(x):\n    if x:\n        raise ValueError('e')\n    val = [x]\n    return val\ndef c():\n    return k(1)\n",
     "def k(x):\n    if x:\n        raise ValueError('e')\n    result = [x]\n    return result\ndef c():\n    return k(1)\n",
     [], ["dropped raise", "new raise", "ValueError"]),
]


def _run(old_src, new_src):
    d = tempfile.mkdtemp(prefix="s1m_")
    with open(os.path.join(d, "m.py"), "w") as f:
        f.write(new_src)
    db = os.path.join(d, "g.db")
    subprocess.run([BIN, "-root", d, "-output", db], capture_output=True, text=True, timeout=60)
    return "\n".join(compute_delta(db, "m.py", repo_root=d,
                                   old_content=old_src, current_content=new_src))


def main():
    rows, tot_fp, tot_tp_exp, tot_tp_hit, det_ok = [], 0, 0, 0, 0
    for name, old, new, expect, forbid in SCENARIOS:
        outs = [_run(old, new) for _ in range(N_RUNS)]
        deterministic = len(set(outs)) == 1
        det_ok += 1 if deterministic else 0
        out = outs[0]
        delta_lines = [l for l in out.splitlines() if l.strip().startswith(("dropped", "new", "return shape"))]
        tp_hit = sum(1 for e in expect if e in out)
        # a delta line is a FALSE positive if it contains any forbidden token
        fp = sum(1 for l in delta_lines if any(fb in l for fb in forbid))
        tot_fp += fp
        tot_tp_exp += len(expect)
        tot_tp_hit += tp_hit
        rows.append({"scenario": name, "deterministic": deterministic,
                     "delta_lines": len(delta_lines), "false_positives": fp,
                     "tp_expected": len(expect), "tp_captured": tp_hit,
                     "output": out.replace("\n", " | ")[:160]})
    n = len(SCENARIOS)
    metrics = {
        "false_positive_rate": round(tot_fp / max(1, sum(r["delta_lines"] for r in rows) or 1), 8),
        "false_positive_count": tot_fp,
        "true_positive_capture": round(tot_tp_hit / max(1, tot_tp_exp), 8),
        "determinism_rate": round(det_ok / n, 8),
        "scenarios": n, "runs_per_scenario": N_RUNS,
        "arviz_false_positives_run4": 17, "arviz_false_positives_now": rows[0]["false_positives"],
    }
    print("=== Stage-1 contract-DELTA metrics (controlled, deterministic) ===")
    for r in rows:
        print(f"  {r['scenario']:18} det={int(r['deterministic'])} "
              f"FP={r['false_positives']} TP={r['tp_captured']}/{r['tp_expected']} :: {r['output']}")
    print("\n=== HEADLINE METRICS (8dp) ===")
    print(json.dumps(metrics, indent=2))
    out_path = os.path.join(os.path.dirname(__file__), "stage1_metrics_20260605.json")
    with open(out_path, "w") as f:
        json.dump({"metrics": metrics, "rows": rows}, f, indent=2)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
