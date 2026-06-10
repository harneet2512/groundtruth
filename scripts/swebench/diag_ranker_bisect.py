#!/usr/bin/env python3
"""Bisect: which ranker stage returns empty for kozea__weasyprint-2300?

Tests in order:
  1. preprocess(issue_text) — query expansion
  2. rank_files(query, repo_path, graph_db) — file ranker
  3. rank_functions(query, ranked_files, repo_path, graph_db) — func ranker
"""
import os
import sys

os.environ.setdefault("GT_V22_TIER1", "1")
os.environ.setdefault("GT_V22_MULTIHOP", "2")
os.environ.setdefault("GT_V22_GLOBAL_BM25", "1")

sys.path.insert(0, "/home/ubuntu/Groundtruth")
sys.path.insert(0, "/home/ubuntu/Groundtruth/src")


def main():
    iid = "kozea__weasyprint-2300"
    graph_db = f"/home/ubuntu/eval_indexes/{iid}/graph.db"
    repo_path = f"/home/ubuntu/eval_repos/{iid}"

    print(f"=== inputs ===")
    print(f"graph_db: {graph_db} (size={os.path.getsize(graph_db)})")
    print(f"repo_path: {repo_path} (isdir={os.path.isdir(repo_path)})")

    from datasets import load_dataset
    ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")
    rows = [r for r in ds if r["instance_id"] == iid]
    issue_text = rows[0]["problem_statement"]
    print(f"issue_text: {len(issue_text)} chars")
    print(f"issue_text[:200]: {issue_text[:200]!r}")

    from groundtruth.pretask.query_preprocessor import preprocess
    print(f"\n=== stage 1: preprocess(issue_text) ===")
    query = preprocess(issue_text)
    print(f"query type: {type(query).__name__}")
    if hasattr(query, "__dict__"):
        for k, v in query.__dict__.items():
            preview = str(v)[:200]
            print(f"  query.{k} = {preview}")
    elif hasattr(query, "_asdict"):
        for k, v in query._asdict().items():
            preview = str(v)[:200]
            print(f"  query.{k} = {preview}")
    else:
        print(f"  query repr: {str(query)[:400]}")

    from groundtruth.pretask.v2_ranker import rank_files, rank_functions
    print(f"\n=== stage 2: rank_files(query, repo_path, graph_db) ===")
    try:
        ranked_files = rank_files(query, repo_path, graph_db)
    except Exception as e:
        import traceback
        print(f"EXCEPTION: {e}\n{traceback.format_exc()}")
        return
    print(f"ranked_files count: {len(ranked_files)}")
    for i, rf in enumerate(ranked_files[:8]):
        print(f"  [{i+1}] {rf.file} score={rf.score:.4f}")

    if not ranked_files:
        print("STOP: ranked_files empty — that's where the brief dies")
        # Try with looser settings — maybe confidence floor too high
        print("\n=== retry with GT_V22_TIER1=0 (no tier1 filtering) ===")
        os.environ["GT_V22_TIER1"] = "0"
        try:
            ranked_files = rank_files(query, repo_path, graph_db)
            print(f"  GT_V22_TIER1=0 → {len(ranked_files)} files")
            for i, rf in enumerate(ranked_files[:5]):
                print(f"    [{i+1}] {rf.file} score={rf.score:.4f}")
        except Exception as e:
            print(f"  EXCEPTION: {e}")
        return

    print(f"\n=== stage 3: rank_functions ===")
    try:
        ranked_funcs = rank_functions(query, ranked_files, repo_path, graph_db)
    except Exception as e:
        import traceback
        print(f"EXCEPTION: {e}\n{traceback.format_exc()}")
        return
    print(f"ranked_funcs count: {len(ranked_funcs)}")
    for i, rf in enumerate(ranked_funcs[:5]):
        print(f"  [{i+1}] {rf.file}:{rf.function} score={rf.score:.4f}")


if __name__ == "__main__":
    main()
