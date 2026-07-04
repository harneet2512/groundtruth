#!/usr/bin/env python3
"""build_verified_manifest.py — build verified_manifest.json for the SWE-bench-Verified proof sweep.

Sources the PUBLIC HF dataset `princeton-nlp/SWE-bench_Verified` (500 instances,
split=test; columns include `instance_id`, `repo`) via the HF datasets-server
REST API — stdlib-only, no token, no `datasets` install needed (same pattern as
scripts/vm/build_pro_manifest.py).

Unlike Pro, Verified carries no docker-image column — the image ref is DERIVED
from the instance_id. Two public registries serve prebuilt x86_64 task images
anonymously (both verified live by anonymous registry-API HEAD, 2026-06-10):

  dockerhub   docker.io/swebench/sweb.eval.x86_64.<iid `__`->`_1776_`>:latest
              (the official prebuilt images; the swebench harness escapes the
               `__` separator as `_1776_` for remote image names — raw `__`
               returns 401/absent on Docker Hub)
  ghcr-epoch  ghcr.io/epoch-research/swe-bench.eval.x86_64.<iid raw `__`>:latest
              (Epoch AI mirror; note `swe-bench.eval` HYPHENATED, raw `__`
               KEPT — the `_1776_` form returns 403/absent on GHCR)

`--registry auto` (default) spot-checks >=3 instances on BOTH registries and
picks ghcr-epoch as primary when it confirms 200s: GHCR has no anonymous pull
rate limit, while Docker Hub caps anonymous pulls (~100/6h/IP) — a 500-image
sweep from one VM IP would exceed that mid-run. Docker Hub is recorded as the
per-task `fallback_image`.

Output shape is exactly what scripts/vm/gt_proof_sweep.sh consumes (the same
contract as pro_manifest.json / artifact_deepswe/repo_manifest.json):

    {"benchmark": ..., "total_tasks": N, "language_distribution": {...},
     "tasks": [{"instance_id", "language", "docker_image", ...}, ...]}

All Verified tasks are Python (12 source repos); `language` is derived from the
known-repo map and defaults to "python" (warn-on-unknown, never empty).

Host-generic: no cloud-provider specifics, no credentials, anonymous HTTP only.

Usage:
    python3 scripts/vm/build_verified_manifest.py --out verified_manifest.json
    python3 scripts/vm/build_verified_manifest.py --max-tasks 10
    python3 scripts/vm/build_verified_manifest.py --spot-check 5
    python3 scripts/vm/build_verified_manifest.py --registry dockerhub
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DATASET = "princeton-nlp/SWE-bench_Verified"
CONFIG = "default"
SPLIT = "test"
ROWS_API = "https://datasets-server.huggingface.co/rows"
INFO_API = "https://datasets-server.huggingface.co/info"
PAGE = 100
UA = {"User-Agent": "gt-proof-sweep-manifest/1.0"}

# The 12 source repos of SWE-bench Verified — all Python. Unknown repos still
# map to python (the dataset is Python-only) but are warned on stderr.
_KNOWN_PY_REPOS = {
    "astropy/astropy", "django/django", "matplotlib/matplotlib",
    "mwaskom/seaborn", "pallets/flask", "psf/requests", "pydata/xarray",
    "pylint-dev/pylint", "pytest-dev/pytest", "scikit-learn/scikit-learn",
    "sphinx-doc/sphinx", "sympy/sympy",
}

# ── image-ref derivation (verified live 2026-06-10, anonymous HEAD = 200) ─────
# swebench harness remote-image naming: `__` in instance_id is escaped `_1776_`
# (Docker Hub `swebench` org). Epoch's GHCR mirror keeps raw `__` and uses the
# hyphenated `swe-bench.eval` prefix. Both tag `latest`, arch x86_64 only.

def dockerhub_name(iid):
    return f"swebench/sweb.eval.x86_64.{iid.lower().replace('__', '_1776_')}"


def ghcr_epoch_name(iid):
    return f"epoch-research/swe-bench.eval.x86_64.{iid.lower()}"


def image_ref(registry, iid):
    if registry == "dockerhub":
        return f"docker.io/{dockerhub_name(iid)}:latest"
    if registry == "ghcr-epoch":
        return f"ghcr.io/{ghcr_epoch_name(iid)}:latest"
    raise ValueError(f"unknown registry: {registry}")


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


def derive_language(repo):
    if repo not in _KNOWN_PY_REPOS:
        print(f"WARN: repo {repo!r} not in the known Verified repo set — "
              f"defaulting language=python (Verified is Python-only)",
              file=sys.stderr)
    return "python"


def build_tasks(rows, primary, fallback, max_tasks=None):
    tasks, seen = [], set()
    for row in rows:
        iid = (row.get("instance_id") or "").strip()
        repo = (row.get("repo") or "").strip()
        if not iid or not repo:
            raise SystemExit(f"FATAL: row missing instance_id/repo: "
                             f"{json.dumps({k: row.get(k) for k in ('instance_id', 'repo')})}")
        if iid in seen:
            raise SystemExit(f"FATAL: duplicate instance_id: {iid}")
        seen.add(iid)
        if iid != iid.lower():
            print(f"WARN: instance_id {iid!r} is not lowercase — image names "
                  f"use the lowercased form", file=sys.stderr)
        tasks.append({
            "instance_id": iid,
            "repo": repo,
            "language": derive_language(repo),
            "docker_image": image_ref(primary, iid),
            "fallback_image": image_ref(fallback, iid),
        })
    if max_tasks:
        tasks = tasks[:max_tasks]
    return tasks


# ── anonymous registry existence check (token -> HEAD manifest) ───────────────
_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])

_REGISTRY_API = {
    # registry -> (token_url_fmt, manifest_url_fmt)
    "dockerhub": (
        "https://auth.docker.io/token?service=registry.docker.io&scope={scope}",
        "https://registry-1.docker.io/v2/{repo}/manifests/{tag}",
    ),
    "ghcr-epoch": (
        "https://ghcr.io/token?service=ghcr.io&scope={scope}",
        "https://ghcr.io/v2/{repo}/manifests/{tag}",
    ),
}


def manifest_exists(registry, repo, tag):
    tok_fmt, man_fmt = _REGISTRY_API[registry]
    scope = urllib.parse.quote(f"repository:{repo}:pull")
    try:
        req = urllib.request.Request(tok_fmt.format(scope=scope), headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            token = json.load(r).get("token", "")
        if not token:
            return False, "no anonymous token"
        head = urllib.request.Request(
            man_fmt.format(repo=repo, tag=urllib.parse.quote(tag)),
            method="HEAD",
            headers={**UA, "Authorization": f"Bearer {token}",
                     "Accept": _MANIFEST_ACCEPT},
        )
        with urllib.request.urlopen(head, timeout=30) as r:
            return r.status == 200, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def spot_check(instance_ids, n, registries):
    """HEAD-check n instances (spread first/…/last) on each registry.

    Returns (results, ok_by_registry): results is a list of per-probe dicts;
    ok_by_registry[reg] is True iff EVERY probe on that registry returned 200.
    """
    if not instance_ids or n <= 0:
        return [], {}
    n = min(n, len(instance_ids))
    if n == 1:
        picks = [instance_ids[0]]
    else:
        step = (len(instance_ids) - 1) / (n - 1)
        picks = [instance_ids[round(i * step)] for i in range(n)]
        picks = list(dict.fromkeys(picks))  # dedupe, keep order
    results, ok_by_registry = [], {}
    namer = {"dockerhub": dockerhub_name, "ghcr-epoch": ghcr_epoch_name}
    for reg in registries:
        all_ok = True
        for iid in picks:
            ok, why = manifest_exists(reg, namer[reg](iid), "latest")
            ref = image_ref(reg, iid)
            print(f"spot-check [{reg}] {ref}: "
                  f"{'EXISTS' if ok else 'MISSING'} ({why})")
            results.append({"registry": reg, "instance_id": iid,
                            "image": ref, "exists": ok, "detail": why})
            all_ok &= ok
        ok_by_registry[reg] = all_ok
    return results, ok_by_registry


def main():
    ap = argparse.ArgumentParser(
        description="Build verified_manifest.json (SWE-bench Verified -> prebuilt public task images)")
    ap.add_argument("--out", default="verified_manifest.json")
    ap.add_argument("--max-tasks", type=int, default=0)
    ap.add_argument("--spot-check", type=int, default=0,
                    help="HEAD-check N image refs anonymously (auto mode "
                         "always checks >=3 — the registry choice needs evidence)")
    ap.add_argument("--registry", choices=("auto", "dockerhub", "ghcr-epoch"),
                    default="auto",
                    help="primary image registry; auto = spot-check both, "
                         "prefer ghcr-epoch (no anonymous pull rate limit)")
    a = ap.parse_args()

    total = fetch_total()
    print(f"dataset {DATASET} split={SPLIT}: {total} examples", file=sys.stderr)
    rows = fetch_rows(total)
    instance_ids = [(r.get("instance_id") or "").strip() for r in rows]

    # ── registry decision (evidence-based in auto mode; fail-closed) ──────────
    n_checks = a.spot_check
    if a.registry == "auto":
        n_checks = max(n_checks, 3)
        results, ok_by = spot_check(instance_ids, n_checks,
                                    ("ghcr-epoch", "dockerhub"))
        if ok_by.get("ghcr-epoch"):
            primary = "ghcr-epoch"
        elif ok_by.get("dockerhub"):
            primary = "dockerhub"
        else:
            raise SystemExit("FATAL: spot-check confirmed NEITHER registry "
                             "serves the probed images anonymously — refusing "
                             "to write a manifest of unpullable refs")
    else:
        primary = a.registry
        results, ok_by = spot_check(instance_ids, n_checks, (primary,))
        if n_checks and not ok_by.get(primary):
            raise SystemExit(f"FATAL: forced registry {primary!r} failed the "
                             f"spot-check — see probes above")
    fallback = "dockerhub" if primary == "ghcr-epoch" else "ghcr-epoch"
    print(f"registry: primary={primary} fallback={fallback}")

    tasks = build_tasks(rows, primary, fallback, max_tasks=a.max_tasks or None)

    dist = {}
    for t in tasks:
        dist[t["language"]] = dist.get(t["language"], 0) + 1
    manifest = {
        "benchmark": "SWE-bench_Verified",
        "source": f"https://huggingface.co/datasets/{DATASET}",
        "registry_primary": primary,
        "registry_fallback": fallback,
        "image_namespaces": {
            "dockerhub": "docker.io/swebench/sweb.eval.x86_64.<iid __->_1776_>:latest",
            "ghcr-epoch": "ghcr.io/epoch-research/swe-bench.eval.x86_64.<iid>:latest",
        },
        "spot_check": results,
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
