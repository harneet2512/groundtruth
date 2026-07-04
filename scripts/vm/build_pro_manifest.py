#!/usr/bin/env python3
"""build_pro_manifest.py — build pro_manifest.json for the SWE-bench-Pro proof sweep.

Sources the PUBLIC HF dataset `ScaleAI/SWE-bench_Pro` (731 instances, split=test;
columns include `instance_id`, `repo`, `repo_language`, `dockerhub_tag`) via the
HF datasets-server REST API — stdlib-only, no token, no `datasets` install needed.
Maps every task to the public GHCR mirror:

    ghcr.io/<owner>/sweap-images:<dockerhub_tag>      (default owner: hbali-stack)

Output shape is exactly what scripts/vm/gt_proof_sweep.sh consumes (the same
contract as artifact_deepswe/repo_manifest.json):

    {"benchmark": ..., "total_tasks": N, "language_distribution": {...},
     "tasks": [{"instance_id", "language", "docker_image", ...}, ...]}

Host-generic: no cloud-provider specifics, no credentials, anonymous HTTP only.

Usage:
    python3 scripts/vm/build_pro_manifest.py --out pro_manifest.json
    python3 scripts/vm/build_pro_manifest.py --max-tasks 10 --languages python,go
    python3 scripts/vm/build_pro_manifest.py --spot-check 2     # verify GHCR refs exist
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DATASET = "ScaleAI/SWE-bench_Pro"
CONFIG = "default"
SPLIT = "test"
ROWS_API = "https://datasets-server.huggingface.co/rows"
INFO_API = "https://datasets-server.huggingface.co/info"
PAGE = 100
UA = {"User-Agent": "gt-proof-sweep-manifest/1.0"}

# Normalize repo_language values to the names the sweep report groups by
# (matches artifact_deepswe/repo_manifest.json conventions).
_LANG_NORM = {
    "js": "javascript", "javascript": "javascript",
    "ts": "typescript", "typescript": "typescript",
    "py": "python", "python": "python",
    "go": "go", "golang": "go",
    "rust": "rust", "rs": "rust",
    "java": "java",
}


def _get_json(url, retries=3):
    last = None
    for i in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 — classified retry, never silent
            last = e
            print(f"WARN: GET {url} attempt {i} failed: {e}", file=sys.stderr)
            time.sleep(5 * i)
    raise SystemExit(f"FATAL: could not fetch {url}: {last}")


def fetch_total():
    j = _get_json(f"{INFO_API}?dataset={urllib.parse.quote(DATASET)}")
    info = (j.get("dataset_info") or {}).get(CONFIG) or {}
    splits = info.get("splits") or {}
    n = (splits.get(SPLIT) or {}).get("num_examples")
    if not n:
        raise SystemExit(f"FATAL: dataset info has no {SPLIT} split count: {j}")
    return int(n)


def fetch_rows(total):
    rows = []
    for offset in range(0, total, PAGE):
        url = (f"{ROWS_API}?dataset={urllib.parse.quote(DATASET)}"
               f"&config={CONFIG}&split={SPLIT}&offset={offset}&length={PAGE}")
        j = _get_json(url)
        page = [r["row"] for r in (j.get("rows") or [])]
        if not page:
            raise SystemExit(f"FATAL: empty rows page at offset {offset}")
        rows.extend(page)
        print(f"  fetched {len(rows)}/{total} rows", file=sys.stderr)
    return rows


def norm_language(row):
    raw = (row.get("repo_language") or "").strip().lower()
    if raw in _LANG_NORM:
        return _LANG_NORM[raw]
    # Repo-name heuristic fallback (coarse, fine per spec); else unknown.
    repo = (row.get("repo") or "").lower()
    for hint, lang in (("go", "go"), ("rust", "rust"), ("node", "javascript"),
                       ("js", "javascript"), ("ts", "typescript"), ("py", "python")):
        if repo.endswith(hint) or f"-{hint}" in repo or f"{hint}-" in repo:
            return lang
    return raw or "unknown"


def build_tasks(rows, ghcr_owner, languages=None, max_tasks=None):
    tasks, seen = [], set()
    for row in rows:
        iid = (row.get("instance_id") or "").strip()
        tag = (row.get("dockerhub_tag") or "").strip()
        if not iid or not tag:
            raise SystemExit(f"FATAL: row missing instance_id/dockerhub_tag: "
                             f"{json.dumps({k: row.get(k) for k in ('instance_id', 'dockerhub_tag')})}")
        if iid in seen:
            raise SystemExit(f"FATAL: duplicate instance_id: {iid}")
        seen.add(iid)
        lang = norm_language(row)
        if languages and lang not in languages:
            continue
        tasks.append({
            "instance_id": iid,
            "repo": row.get("repo", ""),
            "language": lang,
            "docker_image": f"ghcr.io/{ghcr_owner}/sweap-images:{tag}",
            "dockerhub_tag": tag,
        })
    if max_tasks:
        tasks = tasks[:max_tasks]
    return tasks


# ── GHCR existence spot-check (anonymous: token -> HEAD manifest) ─────────────
_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])


def ghcr_manifest_exists(owner, repo, tag):
    scope = f"repository:{owner}/{repo}:pull"
    tok_url = f"https://ghcr.io/token?service=ghcr.io&scope={urllib.parse.quote(scope)}"
    try:
        req = urllib.request.Request(tok_url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            token = json.load(r).get("token", "")
        if not token:
            return False, "no anonymous token"
        head = urllib.request.Request(
            f"https://ghcr.io/v2/{owner}/{repo}/manifests/{urllib.parse.quote(tag)}",
            method="HEAD",
            headers={**UA, "Authorization": f"Bearer {token}", "Accept": _MANIFEST_ACCEPT},
        )
        with urllib.request.urlopen(head, timeout=30) as r:
            return r.status == 200, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def main():
    ap = argparse.ArgumentParser(description="Build pro_manifest.json (SWE-bench-Pro -> GHCR mirror)")
    ap.add_argument("--out", default="pro_manifest.json")
    ap.add_argument("--ghcr-owner", default="hbali-stack")
    ap.add_argument("--max-tasks", type=int, default=0)
    ap.add_argument("--languages", default="",
                    help="comma-separated filter, e.g. python,go (normalized names)")
    ap.add_argument("--spot-check", type=int, default=0,
                    help="HEAD-check N image refs anonymously on GHCR")
    a = ap.parse_args()

    languages = {s.strip().lower() for s in a.languages.split(",") if s.strip()} or None

    total = fetch_total()
    print(f"dataset {DATASET} split={SPLIT}: {total} examples", file=sys.stderr)
    rows = fetch_rows(total)
    tasks = build_tasks(rows, a.ghcr_owner, languages=languages,
                        max_tasks=a.max_tasks or None)

    dist = {}
    for t in tasks:
        dist[t["language"]] = dist.get(t["language"], 0) + 1
    manifest = {
        "benchmark": "SWE-bench_Pro",
        "source": f"https://huggingface.co/datasets/{DATASET}",
        "image_mirror": f"ghcr.io/{a.ghcr_owner}/sweap-images",
        "total_tasks": len(tasks),
        "language_distribution": dict(sorted(dist.items(), key=lambda kv: -kv[1])),
        "tasks": tasks,
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)

    print(f"wrote {a.out}: {len(tasks)} tasks")
    print(f"language_distribution: {manifest['language_distribution']}")
    for t in tasks[:3]:
        print(f"sample: {t['instance_id']} [{t['language']}] -> {t['docker_image']}")

    if a.spot_check:
        picks = [tasks[0], tasks[-1]] if len(tasks) > 1 else tasks[:1]
        picks = picks[: a.spot_check]
        rc = 0
        for t in picks:
            ok, why = ghcr_manifest_exists(a.ghcr_owner, "sweap-images", t["dockerhub_tag"])
            print(f"spot-check {t['docker_image']}: {'EXISTS' if ok else 'MISSING'} ({why})")
            rc |= 0 if ok else 1
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
