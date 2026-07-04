#!/usr/bin/env python
"""Build the HELD-OUT generalization split under D:/gt_runs/localization_heldout/.

Same method as build_localization_testset.py (clone at base_commit + local
gt-index), but for a 12-task split chosen across 12 distinct repos (single .py
gold each) to gate generalization: a lever that helps the 6-case test but
regresses these is REJECTED. Issue text from dataset problem_statement
(deterministic, no run artifact needed). Nothing fabricated.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

OUT_DIR = "D:/gt_runs/localization_heldout"
DATASET = "D:/Groundtruth/benchmarks/data/swebench_live_lite.jsonl"
GT_INDEX = "D:/Groundtruth/gt-index/gt-index.exe"
REPOS_DIR = os.path.join(OUT_DIR, "repos")
GRAPHS_DIR = os.path.join(OUT_DIR, "graphs")

HELDOUT = [
    "delgan__loguru-1306", "beancount__beancount-931", "dynaconf__dynaconf-1241",
    "fonttools__fonttools-3682", "jazzband__tablib-613", "kedro-org__kedro-4387",
    "pydata__xarray-9586", "python-control__python-control-1111",
    "sissbruecker__linkding-989", "theoehrly__fast-f1-699",
    "iterative__dvc-10711", "privacyidea__privacyidea-4223",
]


def _gold_from_patch(patch: str) -> list[str]:
    golds = re.findall(r"^\+\+\+ b/(.+)$", patch or "", re.M)
    return [g.strip() for g in golds if g.strip() and g.strip() != "/dev/null"]


def _run(cmd, cwd=None, timeout=1800):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return 1, repr(e)


def main() -> None:
    ds = {}
    with open(DATASET, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                d = json.loads(line)
                ds[d["instance_id"]] = d
    os.makedirs(os.path.join(OUT_DIR, "tasks"), exist_ok=True)
    os.makedirs(REPOS_DIR, exist_ok=True)
    os.makedirs(GRAPHS_DIR, exist_ok=True)
    for iid in HELDOUT:
        d = ds.get(iid)
        if not d:
            print(f"[WARN] {iid} not in dataset", file=sys.stderr)
            continue
        gold = _gold_from_patch(d.get("patch", ""))
        cfg = {
            "id": iid, "category": "HELDOUT", "repo": d.get("repo", ""),
            "base_commit": d.get("base_commit", ""), "gold_files": gold,
            "issue_text": d.get("problem_statement", ""),
            "issue_text_source": "dataset:problem_statement",
            "graph_db": None, "repo_dir": None,
        }
        repo_dir = os.path.join(REPOS_DIR, iid)
        graph_db = os.path.join(GRAPHS_DIR, f"{iid}.db")
        url = f"https://github.com/{d['repo']}.git"
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            print(f"[CLONE] {iid} <- {url}")
            rc, out = _run(["git", "clone", "--quiet", url, repo_dir], timeout=2400)
            if rc != 0:
                print(f"[FAIL clone] {iid} rc={rc}\n{out[-800:]}", file=sys.stderr)
                cfg["index_status"] = f"clone_failed rc={rc}"
                json.dump(cfg, open(os.path.join(OUT_DIR, "tasks", f"{iid}.json"), "w", encoding="utf-8"), indent=2)
                continue
        rc, out = _run(["git", "-C", repo_dir, "checkout", "--quiet", d["base_commit"]], timeout=300)
        if rc != 0:
            _run(["git", "-C", repo_dir, "fetch", "--quiet", "origin", d["base_commit"]], timeout=600)
            rc, out = _run(["git", "-C", repo_dir, "checkout", "--quiet", d["base_commit"]], timeout=300)
        if rc != 0:
            print(f"[FAIL checkout] {iid} rc={rc}", file=sys.stderr)
            cfg["index_status"] = f"checkout_failed rc={rc}"
            json.dump(cfg, open(os.path.join(OUT_DIR, "tasks", f"{iid}.json"), "w", encoding="utf-8"), indent=2)
            continue
        print(f"[INDEX] {iid} -> {graph_db}")
        if os.path.exists(graph_db):
            os.remove(graph_db)
        rc, out = _run([GT_INDEX, "-root", repo_dir, "-output", graph_db], timeout=3600)
        ok = rc == 0 and os.path.exists(graph_db) and os.path.getsize(graph_db) > 0
        cfg["repo_dir"] = repo_dir
        cfg["graph_db"] = graph_db if ok else None
        cfg["index_status"] = "ok" if ok else f"index_failed rc={rc}: {out[-400:]}"
        json.dump(cfg, open(os.path.join(OUT_DIR, "tasks", f"{iid}.json"), "w", encoding="utf-8"), indent=2)
        print(f"[INDEX {'OK' if ok else 'FAIL'}] {iid} rc={rc} size="
              f"{os.path.getsize(graph_db) if os.path.exists(graph_db) else 0}")


if __name__ == "__main__":
    main()
