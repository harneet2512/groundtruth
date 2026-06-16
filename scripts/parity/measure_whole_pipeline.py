#!/usr/bin/env python3
"""Parity whole-pipeline coverage measurement (steps 1-5, no agent).

Given a built + LSP-enriched graph.db, the extracted repo source, and a DeepSWE
task dir, measure how much of the task's GOLD the WHOLE pipeline surfaces:

  * WHOLE_brief_cov  -- gold basenames mentioned in `generate_v1r_brief(... gold_files=None)`
  * localize recall  -- gold ranks in `localize(...).candidates` (recall@8 / recall@15)

GOLD is parsed from solution/solution.patch HERE, in the MEASUREMENT harness only.
It is NEVER passed into product logic (gold_files=None) -- this is a yardstick,
not a benchmaxx input. The brief/localizer never see the gold.

Emits human-readable WP/CUM lines AND a single machine-readable `PARITY_JSON {...}`
line per task so the workflow's summarize step can aggregate the 5-lang table.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# Exclude test / fixture / generated paths from GOLD: a feature task's gold is the
# product files it changes; test edits are the harness, not the localization target.
_NON_GOLD = re.compile(
    r"(^|/)(test|tests|spec|specs|__tests__|fixtures?|testdata|snapshots?)(/|$)"
    r"|\.(snap|md|txt|lock|json|ya?ml|toml|cfg|ini)$"
    r"|(_test|\.test|\.spec)\.",
    re.IGNORECASE,
)
_PATCH_TGT = re.compile(r"^\+\+\+ b/(.+?)\s*$", re.MULTILINE)
_SRC_FILE = re.compile(r"[\w./\\-]+\.(?:go|py|ts|tsx|js|jsx|rs|java)")


def _gold_basenames(patch_text: str) -> set[str]:
    gold: set[str] = set()
    for raw in _PATCH_TGT.findall(patch_text):
        path = raw.strip().replace("\\", "/")
        if path in ("/dev/null", ""):
            continue
        if _NON_GOLD.search(path):
            continue
        gold.add(os.path.basename(path))
    return gold


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--task-dir", required=True)
    ap.add_argument("--lang", required=True)
    ap.add_argument("--task-id", default="")
    args = ap.parse_args()

    task_id = args.task_id or os.path.basename(args.task_dir.rstrip("/"))
    issue = _read(os.path.join(args.task_dir, "instruction.md"))
    patch = _read(os.path.join(args.task_dir, "solution", "solution.patch"))
    gold = _gold_basenames(patch)
    n_gold = len(gold)

    out: dict[str, object] = {
        "task_id": task_id,
        "lang": args.lang,
        "n_gold": n_gold,
        "gold": sorted(gold),
    }

    # ---- whole pipeline: full brief coverage (gold_files=None -> no leakage) ----
    wp_cov: list[str] = []
    try:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief

        brief = generate_v1r_brief(issue, args.src, args.db, gold_files=None)
        brief_text = brief if isinstance(brief, str) else str(brief)
        mentioned = {os.path.basename(m) for m in _SRC_FILE.findall(brief_text)}
        wp_cov = sorted(g for g in gold if g in mentioned)
        out["whole_brief_cov"] = len(wp_cov)
        out["whole_brief_covered"] = wp_cov
        out["brief_chars"] = len(brief_text)
    except Exception as exc:  # measurement must not crash the matrix leg
        out["whole_brief_error"] = f"{type(exc).__name__}: {exc}"

    # ---- localization recall + REACHABILITY diagnostic (the file RANKER, step 4) ----
    # top_k=500 does NOT change the ranking (sort is before the top_k cut, line ~2730);
    # it only reveals where each gold ACTUALLY ranks. This forks the diagnosis:
    #   * gold in top-500 but rank > 15  -> reachable-but-MISRANKED (a ranking bug to fix)
    #   * gold NOT in top-500 at all      -> not a candidate = UNREACHABLE from issue
    #     anchors (a reachability ceiling; cochange/scope pillars are the lever, not L1)
    try:
        from groundtruth.pretask.graph_localizer import localize

        res = localize(issue, args.db, top_k=500, repo_root=args.src)
        ranked = [os.path.basename(c.file_path) for c in res.candidates]
        full_rank = {}  # gold basename -> 1-indexed full rank, or None if not a candidate
        for g in gold:
            full_rank[g] = (ranked.index(g) + 1) if g in ranked else None
        ranks = sorted(r for r in full_rank.values() if r is not None)
        out["loc_ranks"] = [r for r in ranks if r <= 15]
        out["recall_at_8"] = sum(1 for r in ranks if r <= 8)
        out["recall_at_15"] = sum(1 for r in ranks if r <= 15)
        out["gold_candidates"] = sum(1 for r in full_rank.values() if r is not None)
        out["gold_full_ranks"] = {g: full_rank[g] for g in sorted(gold)}
        out["unreachable_gold"] = sorted(g for g, r in full_rank.items() if r is None)
        out["misranked_gold"] = sorted(
            g for g, r in full_rank.items() if r is not None and r > 15
        )
        out["loc_confident"] = bool(getattr(res, "confident", False))
    except Exception as exc:
        out["loc_error"] = f"{type(exc).__name__}: {exc}"

    wp = out.get("whole_brief_cov", "ERR")
    r8 = out.get("recall_at_8", "ERR")
    r15 = out.get("recall_at_15", "ERR")
    print(
        f"WP  {task_id[:34]:34} [{args.lang[:4]:4}]: N={n_gold:2} "
        f"WHOLE_brief_cov={wp}/{n_gold} covered={wp_cov[:8]}"
    )
    print(
        f"CUM {task_id[:34]:34} [{args.lang[:4]:4}]: N={n_gold:2} "
        f"recall@8={r8}/{n_gold} recall@15={r15}/{n_gold} ranks={out.get('loc_ranks', 'ERR')}"
    )
    print(
        f"DIAG {task_id[:33]:33} [{args.lang[:4]:4}]: candidates={out.get('gold_candidates','ERR')}/{n_gold} "
        f"MISRANKED(in500,>15)={out.get('misranked_gold','ERR')} "
        f"UNREACHABLE(not-cand)={out.get('unreachable_gold','ERR')}"
    )
    print("PARITY_JSON " + json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
