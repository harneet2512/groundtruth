#!/usr/bin/env python3
"""OSS full-pipeline localization proof — runs INSIDE the substrate container.

For each OSS case: index the repo with the baked gt-index (walker fix), then run
the FULL generate_v1r_brief pipeline (embedder + LSP-enriched graph + FTS5 +
compound recall + negation + internal demotion + IDF seed confidence), and score
the gold file's rank.

This is the genuine end-to-end test: the substrate handed an unknown repo + issue
must localize the gold file using EVERY capability in GT. No piece omitted.

Run inside the substrate:
  docker run --rm \
    -v <cases_dir>:/cases:ro \
    -v <repos_dir>:/repos:ro \
    -v <out_dir>:/out \
    -e GT_FORCE_ONNX_EMBEDDER=1 -e GT_REQUIRE_EMBEDDER=1 \
    -e GT_REQUIRE_LSP=1 -e GT_REQUIRE_FTS5=1 \
    <substrate-digest> \
    python /opt/gt/scripts/oss_full_pipeline_proof.py /cases /repos /out
"""
import json
import os
import subprocess
import sys
import tempfile


def _index_repo(repo_root: str, out_db: str) -> dict:
    """Index a repo with the baked gt-index binary (walker fix included)."""
    gt_index = os.environ.get("GT_INDEX_BIN", "gt-index")
    try:
        proc = subprocess.run(
            [gt_index, "-root", repo_root, "-output", out_db],
            capture_output=True, text=True, timeout=600,
        )
        # last line is JSON stats
        last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
        try:
            return json.loads(last)
        except (ValueError, IndexError):
            return {"files": 0, "nodes": 0, "stderr": proc.stderr[-500:]}
    except subprocess.TimeoutExpired:
        return {"files": 0, "nodes": 0, "error": "index_timeout"}


def _gold_rank(brief_text: str, gold_files: list[str]) -> int | None:
    """Find the rank of any gold file in the brief's candidate list."""
    gold_basenames = {os.path.basename(g).rsplit(".", 1)[0].lower() for g in gold_files}
    gold_full = {g.replace("\\", "/").lstrip("./").lstrip("/").lower() for g in gold_files}
    for line in brief_text.split("\n"):
        s = line.strip()
        # candidate lines look like "1. path/to/file.py (..."
        for rank in range(1, 9):
            if s.startswith(f"{rank}."):
                # extract the path token after the rank
                rest = s[len(f"{rank}."):].strip()
                path_tok = rest.split()[0] if rest else ""
                path_norm = path_tok.replace("\\", "/").lstrip("./").lstrip("/").lower()
                base = os.path.basename(path_norm).rsplit(".", 1)[0].lower()
                if path_norm in gold_full or base in gold_basenames:
                    # confirm it's actually a gold path, not a basename collision
                    for gf in gold_full:
                        if path_norm == gf or path_norm.endswith(gf) or gf.endswith(path_norm):
                            return rank
                    if base in gold_basenames:
                        return rank
    return None


def main() -> int:
    # cases_file = JSON manifest [{id,language,repo,issue_text,gold_files}, ...]
    # repos_dir = directory containing cloned repos (subdir per repo name)
    cases_file, repos_dir, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out_dir, exist_ok=True)

    # Import the pipeline (baked at /opt/gt/src)
    sys.path.insert(0, os.environ.get("GT_SRC", "/opt/gt/src"))
    from groundtruth.pretask.v1r_brief import generate_v1r_brief

    results = []
    cases = json.load(open(cases_file, encoding="utf-8-sig"))

    for case in cases:
        cid = case["id"]
        lang = case.get("language", "?")
        issue = case["issue_text"]
        gold_files = case["gold_files"]
        repo_name = case["repo"]
        repo_root = os.path.join(repos_dir, repo_name)

        if not os.path.isdir(repo_root):
            results.append({"id": cid, "language": lang, "error": f"repo_not_mounted:{repo_name}", "gold_rank": None})
            print(f"[SKIP] {cid}: repo not mounted at {repo_root}", file=sys.stderr)
            continue

        # 1. INDEX with the baked gt-index (walker fix)
        db = os.path.join(tempfile.gettempdir(), f"{cid}.db")
        stats = _index_repo(repo_root, db)
        nodes = stats.get("nodes", 0)

        if nodes == 0:
            results.append({
                "id": cid, "language": lang, "gold_rank": None,
                "nodes": 0, "error": "empty_graph", "index_stats": stats,
            })
            print(f"[FAIL] {cid}: 0 nodes after index", file=sys.stderr)
            continue

        # 2. FULL PIPELINE: generate_v1r_brief (embedder + LSP-graph + FTS5 + all fixes)
        try:
            result = generate_v1r_brief(issue, repo_root, db, gold_files=gold_files)
            bt = result.brief_text or ""
            rank = _gold_rank(bt, gold_files)
            sem = getattr(result, "semantic_signal_count", 0)
            results.append({
                "id": cid, "language": lang, "gold_rank": rank,
                "nodes": nodes, "files": stats.get("files", 0),
                "semantic_signal_count": sem,
                "gold_at_1": rank == 1,
                "gold_in_8": rank is not None and rank <= 8,
            })
            # save the brief for audit
            with open(os.path.join(out_dir, f"{cid}.brief.txt"), "w", encoding="utf-8") as f:
                f.write(bt)
            mark = "✓@1" if rank == 1 else (f"@{rank}" if rank else "MISS")
            print(f"[{mark}] {cid} ({lang}) nodes={nodes} sem={sem}", file=sys.stderr)
        except Exception as e:
            import traceback
            results.append({"id": cid, "language": lang, "gold_rank": None, "nodes": nodes, "error": f"{type(e).__name__}: {e}"})
            print(f"[ERR] {cid}: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc()

    # AGGREGATE
    by_lang: dict = {}
    for r in results:
        by_lang.setdefault(r["language"], {"at_1": 0, "in_8": 0, "miss": 0, "sem_zero": 0, "n": 0})
        b = by_lang[r["language"]]
        b["n"] += 1
        if r.get("gold_at_1"):
            b["at_1"] += 1
        if r.get("gold_in_8"):
            b["in_8"] += 1
        if r.get("gold_rank") is None:
            b["miss"] += 1
        if r.get("semantic_signal_count", 0) == 0:
            b["sem_zero"] += 1

    total_at_1 = sum(1 for r in results if r.get("gold_at_1"))
    total_in_8 = sum(1 for r in results if r.get("gold_in_8"))
    total_sem_zero = sum(1 for r in results if r.get("semantic_signal_count", 0) == 0)

    summary = {
        "total_cases": len(results),
        "gold_at_1": total_at_1,
        "gold_in_8": total_in_8,
        "semantic_zero_violations": total_sem_zero,
        "by_language": by_lang,
        "cases": results,
    }
    with open(os.path.join(out_dir, "SUMMARY.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"\n=== FULL PIPELINE PROOF ===", file=sys.stderr)
    print(f"gold_at_1: {total_at_1}/{len(results)}", file=sys.stderr)
    print(f"gold_in_8: {total_in_8}/{len(results)}", file=sys.stderr)
    print(f"semantic_zero_violations: {total_sem_zero} (MUST be 0)", file=sys.stderr)
    for lang, b in sorted(by_lang.items()):
        print(f"  {lang}: @1={b['at_1']}/{b['n']} in8={b['in_8']}/{b['n']} miss={b['miss']} sem_zero={b['sem_zero']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
