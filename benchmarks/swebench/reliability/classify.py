"""classify.py — turn per-surface contracts into ONE final_class.

Surface order; the FIRST failing surface wins, so a failure is attributed to the
deepest infra seam before it can ever be called "GT-quality". Honors the rules:
a tiny/empty LSP demand on an already-deterministic graph is LSP_NO_OP_VALID
(never LSP_FAIL); a gate that fails a healthy substrate is GATE_FALSE_FAIL;
name_match-dominant graph is PRODUCT_QUALITY_FAIL. Read-only.
"""
from __future__ import annotations

import json
import os

FINAL_CLASSES = (
    "GHA_PIPELINE_FAIL", "CONTAINER_RUNTIME_FAIL", "GRAPH_BASE_FAIL",
    "LSP_FAIL", "LSP_NO_OP_VALID", "EMBEDDER_FAIL", "ABSORPTION_FAIL",
    "HOOK_DELIVERY_FAIL", "GATE_FALSE_FAIL", "PRODUCT_QUALITY_FAIL",
    "VALID_INFRA_READY", "GREEN_ROBUST", "GREEN_THIN",
)

# Map the fine final_class to the 6 TOP-LEVEL surfaces the probe report uses.
# GATE_FALSE_FAIL maps to GT_ARCHITECTURE (a gate is a GT component) but is flagged as a
# gate-INVARIANT issue: surfaced + proposed, NEVER auto-tuned to turn a task green.
TOP_LEVEL = {
    "GHA_PIPELINE_FAIL": "GHA_PIPELINE_FAIL",
    "CONTAINER_RUNTIME_FAIL": "CONTAINERIZATION_FAIL",
    "GRAPH_BASE_FAIL": "GT_ARCHITECTURE_FAIL",
    "LSP_FAIL": "GT_ARCHITECTURE_FAIL",
    "LSP_NO_OP_VALID": "GT_ARCHITECTURE_FAIL",
    "EMBEDDER_FAIL": "GT_ARCHITECTURE_FAIL",
    "ABSORPTION_FAIL": "GT_ARCHITECTURE_FAIL",
    "HOOK_DELIVERY_FAIL": "GT_ARCHITECTURE_FAIL",
    "GATE_FALSE_FAIL": "GT_ARCHITECTURE_FAIL",
    "PRODUCT_QUALITY_FAIL": "VALID_SETUP_BUT_PRODUCT_QUALITY_GAP",
    "VALID_INFRA_READY": "VALID_SETUP_BUT_PRODUCT_QUALITY_GAP",
    "GREEN_ROBUST": "GREEN_ROBUST",
    "GREEN_THIN": "GREEN_THIN",
}

# graph hard_fails that mean the base is structurally broken (vs merely under-resolved)
_GRAPH_STRUCTURAL = {"graph_missing", "fts5_missing", "fts5_match_probe_failed",
                     "calls_edges_missing", "schema_version_unexpected"}


def classify_task(c: dict, gate_passed: bool, run_ok: bool = True) -> dict:
    """c: {run, container, graph, lsp, embedder, absorption, hook} contract dicts
    (any may be missing). gate_passed: the run's authoritative 3-GATE verdict for
    this task. Returns {final_class, reason, surfaces}."""
    run = c.get("run", {}) or {}
    cont = c.get("container", {}) or {}
    graph = c.get("graph", {}) or {}
    lsp = c.get("lsp", {}) or {}
    emb = c.get("embedder", {}) or {}
    absorp = c.get("absorption", {}) or {}
    hook = c.get("hook", {}) or {}

    surfaces = {
        "gha_ok": run_ok and not run.get("hard_fail"),
        "container_ok": not cont.get("hard_fail"),
        "graph_structural_ok": not (set(graph.get("hard_fail", [])) & _GRAPH_STRUCTURAL),
        "embedder_ok": not emb.get("hard_fail"),
        "absorption_ok": not absorp.get("absorption_fail"),
        "lsp_real_fail": "lsp_real_failure" in lsp.get("hard_fail", []),
        "lsp_no_op_valid": bool(lsp.get("lsp_no_op_valid")),
        "lsp_did_work": bool(lsp.get("lsp_did_work")),
        "name_match_dominates": bool(graph.get("name_match_dominates")),
        "gate_passed": gate_passed,
    }

    def out(fc, reason):
        return {"final_class": fc, "top_level_class": TOP_LEVEL.get(fc, fc),
                "reason": reason, "surfaces": surfaces}

    # ---- surface order: first failing infra surface wins ----
    if not surfaces["gha_ok"]:
        return out("GHA_PIPELINE_FAIL", f"run_contract hard_fail={run.get('hard_fail')}")
    if not surfaces["container_ok"]:
        return out("CONTAINER_RUNTIME_FAIL", f"container hard_fail={cont.get('hard_fail')}")
    if not surfaces["graph_structural_ok"]:
        return out("GRAPH_BASE_FAIL", f"graph hard_fail={graph.get('hard_fail')}")
    if not surfaces["embedder_ok"]:
        return out("EMBEDDER_FAIL", f"embedder hard_fail={emb.get('hard_fail')}")
    # absorption: semantic scores existed upstream but the join dropped them
    if not surfaces["absorption_ok"]:
        return out("ABSORPTION_FAIL",
                   f"dropped_by_join={absorp.get('dropped_by_join')} "
                   f"examples={absorp.get('dropped_examples', [])[:2]}")
    if surfaces["lsp_real_fail"]:
        return out("LSP_FAIL", f"lsp hard_fail={lsp.get('hard_fail')}")

    # ---- infra surfaces healthy. interpret the gate verdict. ----
    if gate_passed:
        # thin if semantic or LSP fired only marginally
        sem_count = (c.get("gate_metrics", {}) or {}).get("semantic_signal_count")
        thin = (isinstance(sem_count, int) and sem_count <= 1) or \
               (isinstance(lsp.get("resolved"), int) and 0 < lsp["resolved"] <= 2)
        # hook (only meaningful when an agent ran)
        if hook and hook.get("status") in ("DELIVERED_ONLY", "INERT_PAYLOAD", "INCORRECT_PAYLOAD", "NOT_DELIVERED"):
            return out("HOOK_DELIVERY_FAIL", f"hook status={hook.get('status')}")
        return out("GREEN_THIN" if thin else "GREEN_ROBUST",
                   f"all gates ON; det={graph.get('det_pct')} lsp_resolved={lsp.get('resolved')} "
                   f"lsp_no_op_valid={lsp.get('lsp_no_op_valid')}")

    # gate RED, structural surfaces healthy. But if the embedder is ALIVE (3a) yet
    # contributed ZERO score to a non-empty rendered set, the semantic ranking never
    # reached the rendered candidates -> a real consumption/absorption gap
    # (GT_ARCHITECTURE), NOT a false gate. (conan: sem_count=0/5 with cos 0.86.)
    _sc = absorp.get("gate_sem_count")
    if _sc is None:
        _sc = (c.get("gate_metrics", {}) or {}).get("semantic_signal_count")
    _rn = absorp.get("rendered_count")
    if (emb.get("discriminates") and isinstance(_sc, int) and _sc == 0
            and isinstance(_rn, int) and _rn > 0):
        return out("ABSORPTION_FAIL",
                   f"embedder discriminates (3a PASS) but sem_count=0/{_rn} on the rendered "
                   "set -> semantic ranking not consumed into the rendered candidates")

    # gate is RED but every infra surface is healthy -> the gate is failing a sound
    # substrate (false fail) UNLESS the graph is genuinely name_match-dominated.
    if surfaces["name_match_dominates"]:
        return out("PRODUCT_QUALITY_FAIL",
                   f"name_match dominates the call graph (det={graph.get('det_pct')}, "
                   f"name_match={graph.get('name_match_count')} > det={graph.get('deterministic_count')})")
    if surfaces["lsp_no_op_valid"]:
        return out("GATE_FALSE_FAIL",
                   f"gate RED on a healthy substrate; LSP_NO_OP_VALID "
                   f"({lsp.get('lsp_no_op_reason')}); graph det={graph.get('det_pct')}")
    # healthy surfaces + RED gate + no name_match dominance + LSP did real work:
    # the gate's threshold rejected a sound substrate -> still a false fail, flagged.
    return out("GATE_FALSE_FAIL",
               f"gate RED but graph/embedder/absorption healthy and det={graph.get('det_pct')}; "
               f"review the failing gate threshold")


def _load(d: str, name: str):
    p = os.path.join(d, name)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def classify_task_dir(task_dir: str, gate_passed: bool, run_ok: bool = True) -> dict:
    """Load the contract JSONs from a task artifact dir and classify."""
    c = {
        "run": _load(task_dir, "run_contract.json"),
        "container": _load(task_dir, "container_contract.json"),
        "graph": _load(task_dir, "graph_contract.json"),
        "lsp": _load(task_dir, "lsp_contract.json"),
        "embedder": _load(task_dir, "embedder_contract.json"),
        "absorption": _load(task_dir, "absorption_contract.json"),
        "hook": _load(task_dir, "hook_contract.json"),
        "gate_metrics": _load(task_dir, "11_gate_metrics.json"),
    }
    res = classify_task(c, gate_passed, run_ok)
    res["task_dir"] = task_dir
    return res
