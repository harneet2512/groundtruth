"""ABSORPTION contract — does evidence survive the pipeline into the rendered set?

This proves (or refutes) the conan hypothesis: the embedder works, run_v74
computes semantic scores, but the rendered candidates show sem=0 because the
live join in generate_v1r_brief aligns rendered files to run_v74's scores by
EXACT path string (v1r_brief.py:3044) and misses localizer-promoted /
path-normalized files.

It consumes the GT_AUDIT_DIR snapshots emitted by the instrumentation (Build 2),
each a JSON list of candidate records carrying a STABLE candidate_id and, on the
rendered snapshot, BOTH the live (exact-path) semantic alignment AND a
consistent-id alignment. Where live_sem==0 but consistent_sem>0, the score was
dropped by the join — ABSORPTION_FAIL. Read-only.

Snapshot files (per task, in the GT_AUDIT_DIR):
  05_candidates_raw.json  06_candidates_graph.json  07_candidates_lsp_enriched.json
  08_candidates_semantic_scored.json  09_candidates_rrf_merged.json
  10_candidates_rendered.json  11_gate_metrics.json

Rendered record fields the instrumentation emits:
  candidate_id, path, live_join ("MATCH"/"MISS"), live_sem (float),
  consistent_sem (float; sem found by normalized-id lookup), routes (list).
"""
from __future__ import annotations

import json
import os

_STAGES = [
    "05_candidates_raw", "06_candidates_graph", "07_candidates_lsp_enriched",
    "08_candidates_semantic_scored", "09_candidates_rrf_merged",
    "10_candidates_rendered", "11_gate_metrics",
]


def _load(snap_dir: str, name: str):
    p = os.path.join(snap_dir, name + ".json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def build_absorption_contract(snapshot_dir: str) -> dict:
    c: dict = {"contract": "absorption", "snapshot_dir": snapshot_dir}
    present = {s: (_load(snapshot_dir, s) is not None) for s in _STAGES}
    c["stages_present"] = present
    c["snapshots_available"] = any(present.values())
    if not c["snapshots_available"]:
        c["hard_fail"] = ["no_snapshots"]
        c["note"] = "GT_AUDIT_DIR snapshots not found (instrumentation not active for this task)"
        return c

    rendered = _load(snapshot_dir, "10_candidates_rendered") or []
    sem_scored = _load(snapshot_dir, "08_candidates_semantic_scored") or []
    gate = _load(snapshot_dir, "11_gate_metrics") or {}

    # index the semantic-scored stage by candidate_id (the score that SHOULD survive)
    sem_by_id = {}
    for r in sem_scored:
        cid = r.get("candidate_id")
        if cid is not None:
            sem_by_id[cid] = float(r.get("sem", r.get("components", {}).get("sem", 0.0)) or 0.0)

    n = len(rendered)
    live_pos = consistent_pos = dropped = no_lineage = 0
    dropped_examples = []
    for r in rendered:
        cid = r.get("candidate_id")
        live_sem = float(r.get("live_sem", 0.0) or 0.0)
        # consistent_sem: prefer the instrumentation's own consistent-id alignment,
        # else recover it from the semantic-scored snapshot by candidate_id.
        consistent_sem = r.get("consistent_sem", None)
        if consistent_sem is None:
            consistent_sem = sem_by_id.get(cid, 0.0)
        consistent_sem = float(consistent_sem or 0.0)
        routes = r.get("routes") or []
        if live_sem > 0:
            live_pos += 1
        if consistent_sem > 0:
            consistent_pos += 1
        # the bug: the score EXISTS (consistent>0) but the live join lost it (live==0)
        if consistent_sem > 0 and live_sem == 0.0:
            dropped += 1
            if len(dropped_examples) < 6:
                dropped_examples.append({
                    "candidate_id": cid, "path": r.get("path"),
                    "live_join": r.get("live_join"), "live_sem": live_sem,
                    "consistent_sem": round(consistent_sem, 6), "routes": routes,
                })
        if not routes and consistent_sem == 0.0 and cid not in sem_by_id:
            no_lineage += 1

    c["rendered_count"] = n
    c["rendered_with_live_sem"] = live_pos
    c["rendered_with_consistent_sem"] = consistent_pos
    c["dropped_by_join"] = dropped            # consistent>0 but live==0  == the bug
    c["rendered_with_no_lineage"] = no_lineage
    c["dropped_examples"] = dropped_examples

    # the gate's sem_count must be computed from the SAME rendered set
    gate_sem_count = gate.get("semantic_signal_count")
    c["gate_sem_count"] = gate_sem_count
    c["gate_matches_live"] = (gate_sem_count == live_pos) if gate_sem_count is not None else None

    hf: list[str] = []
    # the decisive signal: scores existed upstream and the join dropped them.
    c["absorption_fail"] = dropped > 0
    if dropped > 0:
        hf.append("semantic_scores_dropped_by_join")
    if no_lineage > 0:
        hf.append("rendered_candidate_no_lineage")
    if c.get("gate_matches_live") is False:
        hf.append("gate_scores_different_pool_than_rendered")
    c["hard_fail"] = hf
    return c


if __name__ == "__main__":
    import sys

    d = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gt/audit"
    print(json.dumps(build_absorption_contract(d), indent=2, default=str))
