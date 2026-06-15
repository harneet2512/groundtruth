#!/usr/bin/env python3
"""gt_layer_audit.py - the DEPTH-FIRST, per-LAYER, live-behavioral-gap audit (gt_trial §4).

ONE language-agnostic audit for ANY of the 5 languages. It reads a run's artifacts GENERICALLY
(graph.db edge types + resolution methods, the substrate certs, the agent trajectory tags) and
walks GT layer by layer, STARTING FROM DEPTH (Layer 0), reporting for EACH layer:
    INTENDED (what gt_main/gt_gt says it should do)  vs  ACTUAL (what this run did)  -> FIRED? + GAP.

No per-language logic: edge types, resolution methods, cert fields, and `<gt-...>` trajectory tags
are the SAME contract for py/go/ts/js/rust. The "live behavioral gap" is the delta per layer.

Usage:
    python scripts/gt_layer_audit.py --graph <graph.db> --certs <dir> --trajectory <traj.json> \
        --lang <language> [--task <id>]

Deterministic layers (depth/naming/LSP/embedder) are computed exactly here. The semantic legs
(CONSUMED / RIGHT-TRAJECTORY) are flagged FIRED=yes/no from tag presence; the gt_trial §4 verifier
agent judges consumption from the chronological read - this script gives it the per-layer skeleton.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

# The promoted DEPTH relationship-edge types (Layer 0 completeness, gt_gt §2.6) - language-agnostic.
_DEPTH_EDGES = ("READS", "WRITES", "RAISES", "PRECEDES", "DATA_FLOW", "CO_SERIALIZES")
# The agent-visible GT payload tags, per layer (gt_gt §6).
_LAYER_TAGS = {
    "L1.brief": ("<gt-task-brief>", "<gt-localization>", "<gt-graph-map>", "<gt-orientation>"),
    "L3b.evidence": ("<gt-evidence",),
    "L3.contract": ("<gt-contract",),
    "consensus.scope": ("<gt-scope",),
    "cochange": ("<gt-cochange",),
    "oracle.nudge": ("<gt-nudge",),
}


def _q(cur, sql, *a):
    try:
        return cur.execute(sql, a).fetchone()[0]
    except Exception:
        return None


def audit_depth(graph_db):
    """Layer 0 - DEPTH: relationship-edge completeness + the full edge-type census."""
    c = sqlite3.connect(graph_db).cursor()
    rows = c.execute("SELECT type, COUNT(*) FROM edges GROUP BY type ORDER BY COUNT(*) DESC").fetchall()
    by_type = {t: n for t, n in rows}
    depth_present = [t for t in _DEPTH_EDGES if by_type.get(t, 0) > 0]
    depth_missing = [t for t in _DEPTH_EDGES if by_type.get(t, 0) == 0]
    return {
        "edge_types": by_type,
        "depth_present": depth_present,
        "depth_missing": depth_missing,
        "intended": "all relationship-edge classes present where internally resolvable (READS/WRITES/RAISES/PRECEDES/DATA_FLOW/CO_SERIALIZES)",
        "fired": len(depth_present) > 0,
        "gap": ("none" if not depth_missing else f"missing depth classes: {','.join(depth_missing)}"),
    }


def audit_naming(graph_db):
    """Resolver/NAMING: CALLS resolution - det_pct (resolved facts) vs name_match residual + typing tiers."""
    c = sqlite3.connect(graph_db).cursor()
    calls = _q(c, "SELECT COUNT(*) FROM edges WHERE type='CALLS'") or 0
    breakdown = dict(c.execute(
        "SELECT resolution_method, COUNT(*) FROM edges WHERE type='CALLS' GROUP BY resolution_method").fetchall())
    nm = breakdown.get("name_match", 0)
    det_pct = (100.0 * (calls - nm) / calls) if calls else 0.0
    tiers = {m: breakdown.get(m, 0) for m in ("type_flow", "impl_method", "inherited", "lsp", "verified_unique")}
    return {
        "calls_edges": calls, "name_match": nm, "det_pct": round(det_pct, 4),
        "breakdown": breakdown, "typing_tiers": tiers,
        "intended": "det_pct high (resolved facts dominate), name_match -> 0; method calls resolved via type/LSP, not guessed",
        "fired": calls > 0,
        "gap": ("none" if det_pct >= 80 else f"det_pct {det_pct:.1f}% < 80% - {nm} name_match guesses ({100*nm/max(1,calls):.1f}% of CALLS)"),
    }


def audit_nodes(graph_db):
    """NODE LABELS - confirms type-def nodes exist (the fix#1 surface: Class/ImplBlock rankable)."""
    c = sqlite3.connect(graph_db).cursor()
    labels = dict(c.execute("SELECT label, COUNT(*) FROM nodes GROUP BY label").fetchall())
    typedef = labels.get("Class", 0) + labels.get("ImplBlock", 0)
    return {"labels": labels, "typedef_nodes": typedef,
            "intended": "type definitions (Class/ImplBlock) present AND rankable by the brief (fix#1)",
            "fired": typedef > 0,
            "gap": "none" if typedef > 0 else "no type-def nodes - brief cannot rank type-targeted edits"}


def _load_json(certs_dir, name):
    p = os.path.join(certs_dir, name)
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return None
    return None


def audit_lsp(certs_dir):
    j = _load_json(certs_dir, "lsp_certificate.json") or {}
    warm = bool(j.get("warm_probe_ok") or j.get("lsp_warm"))
    work = j.get("effective_work", j.get("verified_edges", 0) or 0)
    return {"server": j.get("server_command"), "warm": warm, "verified_edges": j.get("verified_edges"),
            "corrected_edges": j.get("corrected_edges"), "effective_work": work,
            "verdict_hint": j.get("verdict_hint"),
            "intended": "LSP server warm + converting >0 method-call edges BEFORE scoring (gt_gt §3)",
            "fired": warm and (work or 0) > 0,
            "gap": "none" if warm and (work or 0) > 0 else f"LSP not converting (warm={warm}, work={work}) - method map stays name_match"}


def audit_embedder(certs_dir):
    j = _load_json(certs_dir, "embedder_certificate.json") or {}
    dim = str(j.get("embedder_dim", ""))
    wsem = j.get("effective_w_sem", 0)
    is_zero = (j.get("all_zero_semantic_reason", "") not in ("", None)) or (wsem in (0, "0", 0.0))
    return {"class": j.get("embedder_class"), "dim": dim, "effective_w_sem": wsem,
            "rendered_nonzero": j.get("rendered_semantic_nonzero_count"),
            "cos_related": j.get("cos_related"), "cos_unrelated": j.get("cos_unrelated"), "is_zero": is_zero,
            "intended": "gte-modernbert 768-d ONNX, effective_w_sem>0, semantic separating (NOT e5, NOT zeroed)",
            "fired": (dim == "768") and not is_zero,
            "gap": "none" if (dim == "768" and not is_zero) else f"embedder degraded (dim={dim}, w_sem={wsem}, zeroed={is_zero})"}


def audit_trajectory(trajectory_path):
    """Per-turn LAYERS - did each layer's tag FIRE in the agent's observations, and how many times."""
    if not trajectory_path or not os.path.isfile(trajectory_path):
        return {"loaded": False, "layers": {}}
    raw = open(trajectory_path, encoding="utf-8", errors="replace").read()
    layers = {}
    for layer, tags in _LAYER_TAGS.items():
        n = sum(raw.count(t) for t in tags)
        layers[layer] = {"fired_count": n, "fired": n > 0,
                         "intended": _LAYER_INTENT.get(layer, ""),
                         "gap": "none" if n > 0 else "DELIVERED=NO - tag never appears in the agent trajectory"}
    # assistant turn count (trajectory length)
    try:
        obj = json.loads(raw)
        msgs = obj if isinstance(obj, list) else obj.get("messages", obj.get("trajectory", []))
        turns = sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "assistant")
    except Exception:
        turns = raw.count('"role": "assistant"') + raw.count('"role":"assistant"')
    return {"loaded": True, "assistant_turns": turns, "layers": layers}


_LAYER_INTENT = {
    "L1.brief": "task-start brief delivered, ranks the gold file high (gt_gt §4/§12)",
    "L3b.evidence": "per-view/per-edit evidence (callers/contracts/siblings) on every edit (gt_gt §6)",
    "L3.contract": "behavioral contract (signature/callers - preserve interface) per edit (gt_gt §6)",
    "consensus.scope": "scope chain fires when the edit fans across components (gt_gt §6)",
    "cochange": "co-change files surfaced from git history (gt_gt §6)",
    "oracle.nudge": "per-turn steer (coherence-collapse/loop/verify) on trigger (gt_gt §15)",
}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_layer_audit")
    ap.add_argument("--graph", required=True)
    ap.add_argument("--certs", default="")
    ap.add_argument("--trajectory", default="")
    ap.add_argument("--lang", default="?")
    ap.add_argument("--task", default="?")
    a = ap.parse_args(argv)
    certs = a.certs or os.path.dirname(a.graph)

    report = {
        "task": a.task, "lang": a.lang,
        "L0_depth": audit_depth(a.graph),
        "naming": audit_naming(a.graph),
        "nodes": audit_nodes(a.graph),
        "lsp": audit_lsp(certs),
        "embedder": audit_embedder(certs),
        "trajectory": audit_trajectory(a.trajectory),
    }

    # ---- render the depth-first per-layer behavioral-gap table ----
    print(f"\n=== GT LAYER AUDIT (depth-first, live behavioral gap) - {a.task} [{a.lang}] ===")
    order = [
        ("L0 DEPTH", report["L0_depth"]),
        ("NAMING/resolver", report["naming"]),
        ("NODES/type-defs", report["nodes"]),
        ("LSP", report["lsp"]),
        ("EMBEDDER", report["embedder"]),
    ]
    print(f"{'LAYER':18} {'FIRED':6} GAP")
    for name, r in order:
        print(f"{name:18} {('YES' if r['fired'] else 'NO '):6} {r['gap']}")
    tr = report["trajectory"]
    if tr.get("loaded"):
        print(f"-- per-turn layers (assistant turns: {tr['assistant_turns']}) --")
        for layer, r in tr["layers"].items():
            print(f"{layer:18} {('YES('+str(r['fired_count'])+')' if r['fired'] else 'NO '):8} {r['gap']}")
    else:
        print("-- per-turn layers: trajectory not provided (substrate-only audit) --")
    print("\n-- detail --")
    print(f"L0 depth edge types: { {k:v for k,v in report['L0_depth']['edge_types'].items()} }")
    print(f"depth present: {report['L0_depth']['depth_present']}  missing: {report['L0_depth']['depth_missing']}")
    print(f"naming det_pct={report['naming']['det_pct']}% name_match={report['naming']['name_match']} tiers={report['naming']['typing_tiers']}")
    print(f"nodes typedef={report['nodes']['typedef_nodes']} labels={report['nodes']['labels']}")
    print(f"lsp warm={report['lsp']['warm']} effective_work={report['lsp']['effective_work']} verdict={report['lsp']['verdict_hint']}")
    print(f"embedder class={report['embedder']['class']} dim={report['embedder']['dim']} w_sem={report['embedder']['effective_w_sem']} zeroed={report['embedder']['is_zero']}")

    out = os.path.join(certs, f"gt_layer_audit_{a.task}.json")
    try:
        json.dump(report, open(out, "w", encoding="utf-8"), indent=1, default=str)
        print(f"\nwrote {out}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
