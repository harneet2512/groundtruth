#!/usr/bin/env python
"""measure_brief.py — the canonical GT localization measurement instrument.

Given (issue_text, graph.db, repo_root, gold_files[]) it calls the AUTHORITATIVE
brief path `generate_v1r_brief` (semantic ON, container ONNX) and reports, per
gold file:
  * the gold's RANK in the brief's delivered candidate list (`.files`), and
    whether it appears at all;
  * the STAGE that dropped it if absent — by re-running the same deterministic
    sub-functions the pipeline uses (run_v74 seeder universe -> exact-name
    guarantee `_exact_issue_named_files` -> localize/RRF candidates -> render
    K-cap = delivered `.files`) and finding the first stage the gold is missing
    from;
  * the full delivered candidate list with scores + the confidence tier;
  * whether `_exact_issue_named_files` fired for the gold (and which symbols).

Semantic MUST be ON (BRIEFING.md): this harness ASSERTS the embedder is the
container ONNX `_OnnxEmbedderAdapter` and ABORTS (or loudly records) otherwise —
never silently falls back to semantic-off. Deterministic / no network.

Usage:
  measure_brief.py --issue-file F --graph G.db --repo R --gold a.py --gold b.py
  measure_brief.py --task-config tasks/<id>.json
  measure_brief.py --testset            # run every task config + emit AGGREGATE
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# --- env: force the container ONNX embedder, point at the baked models ---
os.environ.setdefault("GT_FORCE_ONNX_EMBEDDER", "1")
os.environ.setdefault("GT_REQUIRE_EMBEDDER", "1")
os.environ.setdefault("GT_MODELS_ROOT", "D:/Groundtruth/models")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

sys.path.insert(0, "D:/Groundtruth/src")

from groundtruth.pretask.graph_localizer import _normalize as _norm  # noqa: E402

TESTSET_DIR = "D:/gt_runs/localization_testset"


def _assert_semantic_on() -> dict:
    """Verify the embedder is the container ONNX adapter; never run semantic-off.

    Returns a dict describing the embedder. Raises RuntimeError if it is NOT the
    ONNX adapter (e.g. ZeroEmbeddingoModel / sentence_transformers), per BRIEFING
    §0/§5: a half-on pipeline produces worthless numbers."""
    # Block sentence_transformers explicitly (BRIEFING §0): if importable AND
    # selected it would desync the two halves; the force/require flags already
    # skip it, but we record its presence.
    st_present = False
    try:
        import sentence_transformers  # noqa: F401
        st_present = True
    except Exception:
        st_present = False

    from groundtruth.pretask.v7_4_brief import _OnnxEmbedderAdapter, _get_model

    model = _get_model()
    is_onnx = isinstance(model, _OnnxEmbedderAdapter)
    info = {
        "embedder_class": type(model).__name__,
        "is_onnx_adapter": is_onnx,
        "sentence_transformers_importable": st_present,
        "GT_FORCE_ONNX_EMBEDDER": os.environ.get("GT_FORCE_ONNX_EMBEDDER"),
        "GT_REQUIRE_EMBEDDER": os.environ.get("GT_REQUIRE_EMBEDDER"),
        "GT_MODELS_ROOT": os.environ.get("GT_MODELS_ROOT"),
    }
    if not is_onnx:
        raise RuntimeError(
            "SEMANTIC-OFF ABORT: embedder is "
            f"{type(model).__name__}, not the container ONNX _OnnxEmbedderAdapter. "
            "Per BRIEFING.md any number on a half-on pipeline is worthless. "
            "Ensure onnxruntime is installed and the gte-modernbert ONNX model is "
            f"baked under GT_MODELS_ROOT={os.environ.get('GT_MODELS_ROOT')}."
        )
    # Prove the embedder actually produces a nonzero vector.
    try:
        v = model.encode(["semantic on probe"], normalize_embeddings=True)
        dim = len(v[0]) if hasattr(v, "__len__") and len(v) else 0
        info["probe_dim"] = int(dim)
        info["probe_nonzero"] = bool(dim and any(abs(float(x)) > 1e-9 for x in v[0]))
    except Exception as e:  # noqa: BLE001
        info["probe_error"] = repr(e)
        info["probe_nonzero"] = False
    if not info.get("probe_nonzero"):
        raise RuntimeError(f"SEMANTIC-OFF ABORT: embedder produced zero vector: {info}")
    return info


def _rec_path(r: dict) -> str:
    return _norm(r.get("path") or r.get("file") or r.get("file_path") or "")


def _measure_stages(issue_text: str, repo_root: str, graph_db: str, gold_norm: list[str]) -> dict:
    """Re-run the deterministic stages and record, per gold file, the highest stage
    it survived to. This does NOT alter the authoritative brief; it re-invokes the
    same sub-functions on identical (deterministic) inputs to expose the drop-stage.

    Stage order (pipeline order):
      1. seeder_universe : run_v74().ranked_full  (was it retrieved/scored at all?)
      2. exact_name      : _exact_issue_named_files (the L1 issue-symbol guarantee)
      3. localize_rrf    : localize().candidates    (3-ranker RRF surfaced it?)
      4. render_kcap      : the delivered brief .files (handled by caller)
    """
    stages: dict[str, dict] = {}

    # Stage 1: run_v74 seeder/scoring universe.
    from groundtruth.pretask.v7_4_brief import run_v74

    v74 = run_v74(
        issue_text, repo_root, graph_db,
        ablation="C", k_anchor=3, k_sem_top=10, tau_anchor=0.20,
        max_depth=3, min_confidence=0.7, focus_size=5,
    )
    ranked = v74.ranked_full or []
    rank_by = {}
    for i, r in enumerate(ranked):
        p = _rec_path(r)
        if p not in rank_by:
            rank_by[p] = i
    stages["seeder_universe"] = {
        "count": len(ranked),
        "gold_native_rank": {g: rank_by.get(g, None) for g in gold_norm},
        "effective_w_sem": float(getattr(v74, "effective_w_sem", 0.0) or 0.0),
    }

    # Stage 2: exact-name guarantee.
    from groundtruth.pretask.anchors import extract_issue_anchors
    from groundtruth.pretask.v1r_brief import _exact_issue_named_files

    anchors = None
    try:
        anchors = extract_issue_anchors(issue_text, graph_db)
    except Exception:
        anchors = None
    ein = _exact_issue_named_files(issue_text, graph_db, issue_anchors=anchors)
    ein_norm = {_norm(f): syms for f, syms in ein.items()}
    stages["exact_name"] = {
        "fired_files": sorted(ein_norm.keys()),
        "gold_fired": {g: ein_norm.get(g) for g in gold_norm},
    }

    # Stage 3: localize / 3-way RRF.
    from groundtruth.pretask.graph_localizer import localize

    loc_rank = {}
    loc_info = {}
    try:
        loc = localize(issue_text, graph_db, top_k=8, issue_anchors=anchors, repo_root=repo_root)
        for i, c in enumerate(loc.candidates or []):
            p = _norm(c.file_path)
            if p not in loc_rank:
                loc_rank[p] = i
                loc_info[p] = {
                    "score": round(float(c.score), 8),
                    "confidence": round(float(c.confidence), 8),
                    "verified_witness": bool(c.has_verified_witness),
                    "witness": c.render_witness(),
                }
    except Exception as e:  # noqa: BLE001
        loc_info["_error"] = repr(e)
    stages["localize_rrf"] = {
        "count": len(loc_rank),
        "gold_loc_rank": {g: loc_rank.get(g, None) for g in gold_norm},
        "gold_loc_info": {g: loc_info.get(g) for g in gold_norm},
    }
    return stages


def measure_one(issue_text: str, repo_root: str, graph_db: str, gold_files: list[str]) -> dict:
    """Run the authoritative brief + the stage instrumentation; return a report dict."""
    from groundtruth.pretask.v1r_brief import generate_v1r_brief

    gold_norm = [_norm(g) for g in gold_files]

    # The authoritative delivered brief (semantic ON).
    result = generate_v1r_brief(issue_text, repo_root, graph_db, gold_files=gold_files)
    delivered = result.files or []
    deliv_rank = {}
    candidate_list = []
    for i, e in enumerate(delivered):
        p = _norm(e.path)
        if p not in deliv_rank:
            deliv_rank[p] = i
        candidate_list.append({
            "rank": i,
            "path": e.path,
            "norm": p,
            "score": round(float(e.score), 8),
            "is_gold": p in gold_norm,
            "witness_verified": bool(e.witness_verified),
        })

    stages = _measure_stages(issue_text, repo_root, graph_db, gold_norm)
    seeder = stages["seeder_universe"]
    exact = stages["exact_name"]
    loc = stages["localize_rrf"]

    per_gold = {}
    for g, gn in zip(gold_files, gold_norm):
        delivered_rank = deliv_rank.get(gn, None)
        native_rank = seeder["gold_native_rank"].get(gn)
        loc_r = loc["gold_loc_rank"].get(gn)
        exact_fired = exact["gold_fired"].get(gn)
        # Drop-stage: first pipeline stage (in order) where the gold is absent.
        if delivered_rank is not None:
            drop_stage = "DELIVERED (rank %d)" % delivered_rank
        elif native_rank is None and loc_r is None and not exact_fired:
            drop_stage = "seeder_universe (never retrieved/scored; not in run_v74.ranked_full, not localized, exact-name did not fire)"
        elif loc_r is None and not exact_fired:
            # retrieved by run_v74 but neither localize nor exact-name surfaced it
            drop_stage = "render_kcap (in seeder@%s but not localized, not exact-named -> cut by adaptive-K/render)" % native_rank
        else:
            # it WAS surfaced by localize or exact-name yet still not delivered
            drop_stage = "render_kcap (surfaced by %s but dropped before delivery)" % (
                "localize@%s" % loc_r if loc_r is not None else "exact_name"
            )
        per_gold[g] = {
            "norm": gn,
            "delivered_rank": delivered_rank,
            "delivered": delivered_rank is not None,
            "seeder_native_rank": native_rank,
            "localize_rank": loc_r,
            "localize_info": loc["gold_loc_info"].get(gn),
            "exact_name_fired": bool(exact_fired),
            "exact_name_symbols": exact_fired,
            "drop_stage": drop_stage,
            "tier": result.confidence_tier,
        }

    return {
        "gold_files": gold_files,
        "confidence_tier": result.confidence_tier,
        "delivered_count": len(delivered),
        "delivered_candidates": candidate_list,
        "effective_w_sem": round(float(result.effective_w_sem), 8),
        "semantic_signal_count": int(result.semantic_signal_count),
        "per_gold": per_gold,
        "stages": stages,
    }


def _print_table(task_id: str, rep: dict) -> None:
    print(f"\n=== {task_id} === tier={rep['confidence_tier']} "
          f"delivered={rep['delivered_count']} w_sem={rep['effective_w_sem']} "
          f"sem_signal={rep['semantic_signal_count']}")
    print("  delivered candidates:")
    for c in rep["delivered_candidates"]:
        mark = " <== GOLD" if c["is_gold"] else ""
        print(f"    #{c['rank']:<2} {c['score']:.6f}  {c['path']}{mark}")
    print("  per-gold:")
    for g, info in rep["per_gold"].items():
        print(f"    GOLD {g}")
        print(f"       delivered_rank = {info['delivered_rank']}  "
              f"seeder_rank = {info['seeder_native_rank']}  "
              f"localize_rank = {info['localize_rank']}  "
              f"exact_name_fired = {info['exact_name_fired']}")
        print(f"       DROP-STAGE: {info['drop_stage']}")


def _load_task_configs(testset_dir: str = TESTSET_DIR) -> list[dict]:
    tasks_dir = os.path.join(testset_dir, "tasks")
    out = []
    for fn in sorted(os.listdir(tasks_dir)):
        if fn.endswith(".json"):
            out.append(json.load(open(os.path.join(tasks_dir, fn), encoding="utf-8")))
    return out


def run_task(cfg: dict, env_info: dict) -> dict:
    iid = cfg["id"]
    graph_db = cfg.get("graph_db")
    repo_dir = cfg.get("repo_dir")
    gold = cfg.get("gold_files", [])
    issue = cfg.get("issue_text", "")
    if not graph_db or not os.path.exists(graph_db):
        rep = {
            "task_id": iid,
            "category": cfg.get("category"),
            "status": "graph.db unavailable",
            "graph_db": graph_db,
            "gold_files": gold,
        }
        print(f"\n=== {iid} === graph.db UNAVAILABLE — skipped ({cfg.get('index_status')})")
        return rep
    rep = measure_one(issue, repo_dir or "", graph_db, gold)
    rep["task_id"] = iid
    rep["category"] = cfg.get("category")
    rep["status"] = "measured"
    rep["graph_db"] = graph_db
    rep["env"] = env_info
    _print_table(iid, rep)
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue-file")
    ap.add_argument("--graph")
    ap.add_argument("--repo")
    ap.add_argument("--gold", action="append", default=[])
    ap.add_argument("--task-config")
    ap.add_argument("--testset", action="store_true")
    ap.add_argument("--testset-dir", default=TESTSET_DIR,
                    help="override the testset dir (tasks/ + out) for held-out splits")
    ap.add_argument("--out")
    args = ap.parse_args()

    env_info = _assert_semantic_on()
    print("[ENV] semantic ON:", json.dumps(env_info))

    if args.testset:
        results = []
        for cfg in _load_task_configs(args.testset_dir):
            results.append(run_task(cfg, env_info))
        agg = {"env": env_info, "tasks": results}
        out = args.out or os.path.join(args.testset_dir, "measure_results.json")
        json.dump(agg, open(out, "w", encoding="utf-8"), indent=2, default=str)
        print(f"\n[WROTE] {out}")
        return

    if args.task_config:
        cfg = json.load(open(args.task_config, encoding="utf-8"))
        rep = run_task(cfg, env_info)
        out = args.out or os.path.join(TESTSET_DIR, f"measure_{cfg['id']}.json")
        json.dump(rep, open(out, "w", encoding="utf-8"), indent=2, default=str)
        print(f"\n[WROTE] {out}")
        return

    if not (args.issue_file and args.graph and args.repo):
        ap.error("provide --task-config / --testset, or --issue-file --graph --repo [--gold ...]")
    issue = open(args.issue_file, encoding="utf-8").read()
    rep = measure_one(issue, args.repo, args.graph, args.gold)
    rep["env"] = env_info
    _print_table(os.path.basename(args.graph), rep)
    if args.out:
        json.dump(rep, open(args.out, "w", encoding="utf-8"), indent=2, default=str)
        print(f"\n[WROTE] {args.out}")


if __name__ == "__main__":
    main()
