#!/usr/bin/env python
"""Build the FROZEN localization test set under D:/gt_runs/localization_testset/.

For each frozen task it writes a per-task config (issue_text + gold_files +
repo/base_commit) into tasks/<id>.json, then (optionally) clones the repo at
base_commit and runs the local gt-index to produce graph.db.

issue_text source priority (per MEASURE_BRIEF_PROTOCOL / task spec):
  1. run-artifact output.jsonl  -> instance.problem_statement (the issue the
     agent actually received), else top-level `instruction`.
  2. dataset swebench_live_lite.jsonl problem_statement (deterministic fallback).
gold source: the patch `+++ b/<path>` lines in the dataset (authoritative).

NO network is required to write the configs; cloning/indexing is a separate phase
(--clone / --index) so the frozen config is reproducible offline. Anything that
cannot be obtained is recorded honestly (graph_db: null / "unavailable"); nothing
is fabricated.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

TESTSET_DIR = "D:/gt_runs/localization_testset"
DATASET = "D:/Groundtruth/benchmarks/data/swebench_live_lite.jsonl"
ART_ROOT = "D:/gt_runs/300_e51cc3f0/art_clean"
GT_INDEX = "D:/Groundtruth/gt-index/gt-index.exe"
REPOS_DIR = os.path.join(TESTSET_DIR, "repos")
GRAPHS_DIR = os.path.join(TESTSET_DIR, "graphs")

# Frozen task set. category: MISSED (L1 buried gold) / CONTROL (L1 got gold).
FROZEN_TASKS = [
    {"id": "conan-io__conan-17292", "category": "MISSED"},
    {"id": "streamlink__streamlink-6389", "category": "MISSED"},
    {"id": "conan-io__conan-17517", "category": "MISSED"},
    {"id": "reata__sqllineage-694", "category": "MISSED"},
    {"id": "python-babel__babel-1163", "category": "CONTROL"},
    {"id": "conan-io__conan-17819", "category": "CONTROL"},
]


def _load_dataset() -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(DATASET, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            out[d["instance_id"]] = d
    return out


def _gold_from_patch(patch: str) -> list[str]:
    golds = re.findall(r"^\+\+\+ b/(.+)$", patch or "", re.M)
    # strip trailing whitespace / dev-null
    return [g.strip() for g in golds if g.strip() and g.strip() != "/dev/null"]


def _find_output_jsonl(task_id: str) -> str | None:
    base = os.path.join(ART_ROOT, f"task-{task_id}")
    if not os.path.isdir(base):
        return None
    for root, _dirs, files in os.walk(base):
        if "output.jsonl" in files:
            return os.path.join(root, "output.jsonl")
    return None


def _issue_text_from_artifact(task_id: str) -> tuple[str | None, str]:
    """Return (issue_text, source). Prefer instance.problem_statement from the
    run's output.jsonl; else top-level instruction; else (None, '')."""
    p = _find_output_jsonl(task_id)
    if not p:
        return None, ""
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                inst = d.get("instance")
                if isinstance(inst, dict):
                    ps = inst.get("problem_statement")
                    if ps:
                        return ps, f"output.jsonl:instance.problem_statement ({p})"
                instr = d.get("instruction")
                if instr:
                    return instr, f"output.jsonl:instruction ({p})"
                break
    except Exception as e:  # noqa: BLE001
        return None, f"error reading {p}: {e!r}"
    return None, ""


def write_configs() -> list[dict]:
    ds = _load_dataset()
    os.makedirs(os.path.join(TESTSET_DIR, "tasks"), exist_ok=True)
    cfgs: list[dict] = []
    for t in FROZEN_TASKS:
        iid = t["id"]
        d = ds.get(iid)
        if not d:
            print(f"[WARN] {iid} not in dataset — skipping", file=sys.stderr)
            continue
        gold = _gold_from_patch(d.get("patch", ""))
        issue_art, src_art = _issue_text_from_artifact(iid)
        if issue_art:
            issue_text, issue_src = issue_art, src_art
        else:
            issue_text = d.get("problem_statement", "")
            issue_src = "dataset:problem_statement"
        cfg = {
            "id": iid,
            "category": t["category"],
            "repo": d.get("repo", ""),
            "base_commit": d.get("base_commit", ""),
            "gold_files": gold,
            "issue_text": issue_text,
            "issue_text_source": issue_src,
            "graph_db": None,  # filled by --index
            "repo_dir": None,
        }
        cfgs.append(cfg)
        path = os.path.join(TESTSET_DIR, "tasks", f"{iid}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        print(f"[CONFIG] {iid}  gold={gold}  issue_src={issue_src}  issue_len={len(issue_text)}")
    return cfgs


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 1800) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return 1, repr(e)


def clone_and_index() -> None:
    os.makedirs(REPOS_DIR, exist_ok=True)
    os.makedirs(GRAPHS_DIR, exist_ok=True)
    tasks_dir = os.path.join(TESTSET_DIR, "tasks")
    for fn in sorted(os.listdir(tasks_dir)):
        if not fn.endswith(".json"):
            continue
        cfg_path = os.path.join(tasks_dir, fn)
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        iid = cfg["id"]
        repo = cfg["repo"]
        base = cfg["base_commit"]
        repo_dir = os.path.join(REPOS_DIR, iid)
        graph_db = os.path.join(GRAPHS_DIR, f"{iid}.db")
        url = f"https://github.com/{repo}.git"
        # Clone (fresh, full so any base_commit is reachable) then checkout base.
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            print(f"[CLONE] {iid} <- {url}")
            rc, out = _run(["git", "clone", "--quiet", url, repo_dir], timeout=2400)
            if rc != 0:
                print(f"[FAIL clone] {iid} rc={rc}\n{out[-1500:]}", file=sys.stderr)
                cfg["graph_db"] = None
                cfg["index_status"] = f"clone_failed rc={rc}"
                json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), indent=2)
                continue
        print(f"[CHECKOUT] {iid} -> {base}")
        rc, out = _run(["git", "-C", repo_dir, "checkout", "--quiet", base], timeout=300)
        if rc != 0:
            # try fetching the exact commit then checkout
            _run(["git", "-C", repo_dir, "fetch", "--quiet", "origin", base], timeout=600)
            rc, out = _run(["git", "-C", repo_dir, "checkout", "--quiet", base], timeout=300)
        if rc != 0:
            print(f"[FAIL checkout] {iid} rc={rc}\n{out[-1500:]}", file=sys.stderr)
            cfg["graph_db"] = None
            cfg["index_status"] = f"checkout_failed rc={rc}"
            json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), indent=2)
            continue
        # Index.
        print(f"[INDEX] {iid} -> {graph_db}")
        if os.path.exists(graph_db):
            os.remove(graph_db)
        rc, out = _run([GT_INDEX, "-root", repo_dir, "-output", graph_db], timeout=3600)
        ok = rc == 0 and os.path.exists(graph_db) and os.path.getsize(graph_db) > 0
        cfg["repo_dir"] = repo_dir
        cfg["graph_db"] = graph_db if ok else None
        cfg["index_status"] = "ok" if ok else f"index_failed rc={rc}: {out[-500:]}"
        json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), indent=2)
        print(f"[INDEX {'OK' if ok else 'FAIL'}] {iid} rc={rc} size="
              f"{os.path.getsize(graph_db) if os.path.exists(graph_db) else 0}")
        if not ok:
            print(out[-1500:], file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clone", action="store_true", help="clone repos + run gt-index")
    args = ap.parse_args()
    write_configs()
    if args.clone:
        clone_and_index()


if __name__ == "__main__":
    main()
