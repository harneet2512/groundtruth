"""Orchestrator — run the in-container contract emitters and write JSON.

Invoked INSIDE the eval container by gt-substrate-run.sh AFTER the substrate
(index -> resolve -> gates) has run, so graph.db + lsp_metrics + the GT_AUDIT
snapshots all exist. Writes graph/lsp/embedder/absorption/container contracts +
a summary.json into --out, which gt-substrate-run.sh copies out to the host.

Pure consumer of already-produced artifacts; changes no GT behavior.
"""
from __future__ import annotations

import argparse
import json
import os

from .absorption_contract import build_absorption_contract
from .container_contract import build_container_contract
from .embedder_contract import build_embedder_contract
from .graph_contract import build_graph_contract
from .lsp_contract import build_lsp_contract


def _write(out_dir: str, name: str, obj) -> None:
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)


def emit_all(task_id: str, repo_root: str, graph_db: str, lsp_metrics: str,
             snapshot_dir: str, out_dir: str, closure_before: int | None = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)

    graph_c = build_graph_contract(graph_db, closure_before)
    _write(out_dir, "graph_contract.json", graph_c)

    det_frac = float(graph_c.get("det_pct", 0.0) or 0.0) / 100.0
    lsp_c = build_lsp_contract(lsp_metrics, det_frac)
    _write(out_dir, "lsp_contract.json", lsp_c)

    emb_c = build_embedder_contract()
    _write(out_dir, "embedder_contract.json", emb_c)

    abs_c = build_absorption_contract(snapshot_dir)
    _write(out_dir, "absorption_contract.json", abs_c)

    cont_c = build_container_contract(task_id, repo_root, graph_db)
    _write(out_dir, "container_contract.json", cont_c)

    summary = {
        "task_id": task_id,
        "hard_fail": {
            "graph": graph_c.get("hard_fail", []),
            "lsp": lsp_c.get("hard_fail", []),
            "embedder": emb_c.get("hard_fail", []),
            "absorption": abs_c.get("hard_fail", []),
            "container": cont_c.get("hard_fail", []),
        },
        "signals": {
            "det_pct": graph_c.get("det_pct"),
            "name_match_dominates": graph_c.get("name_match_dominates"),
            "lsp_resolved": lsp_c.get("resolved"),
            "lsp_residual": lsp_c.get("residual"),
            "lsp_no_op_valid": lsp_c.get("lsp_no_op_valid"),
            "lsp_did_work": lsp_c.get("lsp_did_work"),
            "embedder_discriminates": emb_c.get("discriminates"),
            "absorption_fail": abs_c.get("absorption_fail"),
            "dropped_by_join": abs_c.get("dropped_by_join"),
            "container_flags_all_set": cont_c.get("flags_all_set"),
            "import_from_opt_gt": cont_c.get("import_from_opt_gt"),
            "graph_built_in_container": cont_c.get("graph_built_in_container"),
        },
    }
    _write(out_dir, "summary.json", summary)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--graph-db", default="/tmp/gt/graph.db")
    ap.add_argument("--lsp-metrics", default="/tmp/gt/lsp_metrics.txt")
    ap.add_argument("--snapshot-dir", default="/tmp/gt/audit")
    ap.add_argument("--out", default="/tmp/gt/contracts")
    ap.add_argument("--closure-before", type=int, default=None)
    a = ap.parse_args()
    s = emit_all(a.task_id, a.repo_root, a.graph_db, a.lsp_metrics,
                 a.snapshot_dir, a.out, a.closure_before)
    print("RELIABILITY_SUMMARY " + json.dumps(s.get("signals", {})))
    # never fail the substrate on a contract-emission hiccup; the gate rc is authoritative
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
