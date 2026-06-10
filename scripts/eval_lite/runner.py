"""SWE-Bench Lite localization runner — Track D.

Multiprocessing dispatch: one worker per task. Each worker preprocesses the
issue text, ranks files/functions via Track B, extracts gold from the patch,
computes Acc@k, classifies failures, and returns a per-task record.

Aggregates and writes results.json and per-task records under --out.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any


def graph_db_for(instance: dict[str, Any], root: str) -> str:
    repo_safe = instance["repo"].replace("/", "__")
    commit_short = instance["base_commit"][:8]
    return f"{root}/{repo_safe}@{commit_short}/graph.db"


def repo_for(instance: dict[str, Any], root: str) -> str:
    repo_safe = instance["repo"].replace("/", "__")
    return f"{root}/{repo_safe}"


def _classify(query: Any, ranked: Any, gold: dict[str, Any]) -> str:
    high_signal = list(getattr(query, "high_signal_tokens", []) or [])
    file_hints = list(getattr(query, "file_hints", []) or [])
    if len(high_signal) == 0 and len(file_hints) == 0:
        return "wrong_query_terms"
    if len(high_signal) < 3:
        return "issue_text_uninformative"

    pred_files = [getattr(f, "file", "") for f in getattr(ranked, "files", [])]
    pred_funcs = [
        (getattr(f, "file", ""), getattr(f, "function", ""))
        for f in getattr(ranked, "functions", [])
    ]
    gold_files = gold.get("files") or set()
    gold_funcs = gold.get("functions") or set()

    file_hit = bool(set(pred_files[:5]) & gold_files) if gold_files else False
    func_hit = bool(set(pred_funcs[:10]) & gold_funcs) if gold_funcs else False
    if file_hit or func_hit:
        return "hit"

    if gold_funcs:
        in_candidate = any(t in pred_funcs for t in gold_funcs)
        if in_candidate:
            return "ranking_too_low"
    if gold_files:
        in_candidate_f = any(f in pred_files for f in gold_files)
        if in_candidate_f:
            return "ranking_too_low"

    return _classify_graph_miss(gold)


def _classify_graph_miss(gold: dict[str, Any]) -> str:
    graph_db_path = gold.get("_graph_db_path")
    gold_funcs = gold.get("functions") or set()
    if not graph_db_path or not os.path.isfile(graph_db_path):
        return "graph_missing_edge"
    if not gold_funcs:
        return "graph_missing_edge"
    try:
        conn = sqlite3.connect(graph_db_path)
    except sqlite3.Error:
        return "graph_missing_edge"
    try:
        for file_path, fn_name in gold_funcs:
            cur = conn.execute(
                "SELECT 1 FROM nodes WHERE file_path = ? "
                "AND label IN ('Function','Method') AND name = ? LIMIT 1",
                (file_path, fn_name),
            )
            if cur.fetchone() is None:
                cur2 = conn.execute(
                    "SELECT 1 FROM nodes WHERE file_path = ? LIMIT 1",
                    (file_path,),
                )
                if cur2.fetchone() is not None:
                    return "gold_function_not_indexed"
                return "graph_missing_edge"
        return "ranking_too_low"
    finally:
        conn.close()


def _worker(args: tuple[Any, ...]) -> dict[str, Any]:
    instance, graph_db_path, repo_path = args
    instance_id = instance.get("instance_id", "<unknown>")
    try:
        from groundtruth.pretask.query_preprocessor import preprocess
        from groundtruth.pretask.v2_ranker import rank
        from scripts.eval_lite.acc_at_k import (
            hit_at_k_files,
            hit_at_k_functions,
        )
        from scripts.eval_lite.gold_extractor import extract_gold

        issue_text = instance.get("problem_statement") or ""
        query = preprocess(issue_text)
        try:
            ranked = rank(query, repo_path, graph_db_path)
        except Exception as e:
            print(
                f"runner: rank failed for {instance_id}: {e}",
                file=sys.stderr,
            )
            return {"instance_id": instance_id, "error": f"rank: {e}"}

        gold = extract_gold(instance, graph_db_path)
        gold["_graph_db_path"] = graph_db_path

        pred_files = [getattr(f, "file", "") for f in getattr(ranked, "files", [])]
        pred_funcs = [
            (getattr(f, "file", ""), getattr(f, "function", ""))
            for f in getattr(ranked, "functions", [])
        ]

        fail_cat = _classify(query, ranked, gold)

        return {
            "instance_id": instance_id,
            "hit_file_at_1": hit_at_k_files(pred_files, gold["files"], 1),
            "hit_file_at_3": hit_at_k_files(pred_files, gold["files"], 3),
            "hit_file_at_5": hit_at_k_files(pred_files, gold["files"], 5),
            "hit_func_at_5": hit_at_k_functions(pred_funcs, gold["functions"], 5),
            "hit_func_at_10": hit_at_k_functions(pred_funcs, gold["functions"], 10),
            "fail_category": fail_cat,
            "predicted_top5": pred_files[:5],
            "predicted_funcs10": pred_funcs[:10],
            "gold_files": sorted(gold["files"]),
            "gold_functions": sorted([list(t) for t in gold["functions"]]),
        }
    except Exception as e:
        print(
            f"runner: worker crashed for {instance_id}: {e}\n"
            f"{traceback.format_exc()}",
            file=sys.stderr,
        )
        return {"instance_id": instance_id, "error": str(e)}


def _load_dataset(
    cache_dir: str,
    dataset: str = "princeton-nlp/SWE-bench_Lite",
    split: str = "test",
) -> list[dict[str, Any]]:
    if os.path.isdir(cache_dir):
        from datasets import load_from_disk  # type: ignore[import-untyped]

        ds = load_from_disk(cache_dir)
        return [dict(row) for row in ds]
    from datasets import load_dataset  # type: ignore[import-untyped]

    ds = load_dataset(dataset, split=split)  # type: ignore[arg-type]
    return [dict(row) for row in ds]  # type: ignore[misc]


def _aggregate(per_task: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = (
        "hit_file_at_1",
        "hit_file_at_3",
        "hit_file_at_5",
        "hit_func_at_5",
        "hit_func_at_10",
    )
    out: dict[str, Any] = {}
    for m in metrics:
        valid = [r[m] for r in per_task if r.get(m) is not None]
        out[f"acc_{m[4:]}" if m.startswith("hit_") else f"acc_{m}"] = (
            sum(1 for v in valid if v) / len(valid) if valid else 0.0
        )

    fail_hist: dict[str, int] = {}
    for r in per_task:
        cat = r.get("fail_category")
        if cat:
            fail_hist[cat] = fail_hist.get(cat, 0) + 1
    out["fail_category_hist"] = fail_hist

    out["error_count"] = sum(1 for r in per_task if "error" in r)
    out["task_count"] = len(per_task)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--instance-ids", default=None,
                   help="Comma-separated subset of instance ids")
    p.add_argument("--all", action="store_true",
                   help="Run the full SWE-Bench Lite test split")
    p.add_argument("--workers", type=int, default=mp.cpu_count())
    p.add_argument("--out", required=True, help="Output directory")
    p.add_argument("--graph-db-root", default="/data1tb/gt_indexes")
    p.add_argument("--repo-root", default="/data1tb/swe_lite_repos")
    p.add_argument("--dataset-cache",
                   default="/data1tb/swe_lite_dataset")
    p.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite",
                   help="HF dataset id (e.g. SWE-bench-Live/SWE-bench-Live)")
    p.add_argument("--split", default="test",
                   help="Dataset split (e.g. test, lite)")
    args = p.parse_args()

    if not args.all and not args.instance_ids:
        print("runner: must pass --all or --instance-ids", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = _load_dataset(args.dataset_cache, args.dataset, args.split)
    if args.instance_ids:
        wanted = {x.strip() for x in args.instance_ids.split(",") if x.strip()}
        rows = [r for r in rows if r.get("instance_id") in wanted]
    print(f"runner: dispatching {len(rows)} tasks across {args.workers} workers",
          file=sys.stderr)

    tasks = [
        (
            row,
            graph_db_for(row, args.graph_db_root),
            repo_for(row, args.repo_root),
        )
        for row in rows
    ]

    if args.workers <= 1:
        per_task = [_worker(t) for t in tasks]
    else:
        with mp.Pool(args.workers) as pool:
            per_task = pool.map(_worker, tasks)

    per_task.sort(key=lambda r: r.get("instance_id", ""))
    aggregate = _aggregate(per_task)

    results_path = out_dir / "results.json"
    results_path.write_text(
        json.dumps(
            {"per_task": per_task, "aggregate": aggregate},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(f"runner: wrote {results_path}", file=sys.stderr)
    print(json.dumps(aggregate, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
