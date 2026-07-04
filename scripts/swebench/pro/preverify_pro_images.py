#!/usr/bin/env python3
"""
SWE-bench Pro image existence pre-verify (GHA-parity, daemon-free).

Mirrors the SWE-bench-Live workflow's
  "Pre-verify all task images exist (manifest-only, fail-closed)"
step, but for the Pro benchmark and WITHOUT a docker daemon: it checks each
image's existence via the Docker Hub registry API instead of
`docker manifest inspect`, so it runs anywhere (incl. Windows, no Docker).

Pro images live at a SINGLE repo:  docker.io/jefzda/sweap-images:<dockerhub_tag>
where <dockerhub_tag> is taken VERBATIM from the dataset's `dockerhub_tag`
column (no reconstruction, unlike Live's sweb.eval.x86_64.<repo>_1776_<rest>).

Outputs (timestamped, per the artifact-naming rule):
  - <out>/pro_image_manifest_<UTCSTAMP>.jsonl   one row per task: id/lang/tag/exists/size
  - <out>/pro_image_preverify_<UTCSTAMP>.json   summary (counts, per-lang, total GB, missing)

Exit code: 0 if ALL queried images exist (fail-closed parity), 1 if any missing.

Usage:
  # materialize tags from HF + verify all 731 public tasks
  python preverify_pro_images.py --out .claude/reports/pro

  # only a multilingual slice (e.g. 3 each of py/go/js/ts) — for the dev slice
  python preverify_pro_images.py --out .claude/reports/pro --per-lang 3

  # use an already-materialized dataset jsonl instead of hitting HF
  python preverify_pro_images.py --dataset-jsonl /tmp/pro.jsonl --out .claude/reports/pro
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HF_ROWS = "https://datasets-server.huggingface.co/rows"
HUB_TAGS_LIST = "https://hub.docker.com/v2/repositories/jefzda/sweap-images/tags"
DATASET = "ScaleAI/SWE-bench_Pro"
IMAGE_REPO = "docker.io/jefzda/sweap-images"
FIELDS = ("instance_id", "repo", "repo_language", "dockerhub_tag")


def _get_json(url: str, attempts: int = 6, timeout: int = 30):
    """GET url -> (status, parsed-json-or-None). 404 returns (404, None) without raising.
    Honors 429 with a longer backoff (Docker Hub anon rate limit)."""
    last = None
    for a in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, None
            last = e
            time.sleep(min(a * 5, 30) if e.code == 429 else min(a * 2, 8))
            continue
        except Exception as e:  # noqa: BLE001 - transient net
            last = e
        time.sleep(min(a * 2, 8))
    raise RuntimeError(f"GET failed after {attempts}: {url} ({last})")


def fetch_all_hub_tags() -> dict[str, int]:
    """Page the tags-LIST endpoint once (~11 calls for 1002 tags) -> {name: full_size}.
    Far cheaper than one call per dataset tag (which 429s), and gives sizes for free."""
    out: dict[str, int] = {}
    page = 1
    while True:
        q = urllib.parse.urlencode({"page_size": 100, "page": page})
        status, data = _get_json(f"{HUB_TAGS_LIST}?{q}")
        if status == 404 or not data:
            break
        for t in data.get("results", []):
            name = t.get("name")
            if name:
                out[name] = int(t.get("full_size") or 0)
        if not data.get("next"):
            break
        page += 1
    return out


def materialize_tags() -> list[dict]:
    """Page the HF datasets-server /rows API (no `datasets` lib needed)."""
    rows: list[dict] = []
    offset, length = 0, 100
    while True:
        q = urllib.parse.urlencode(
            {"dataset": DATASET, "config": "default", "split": "test",
             "offset": offset, "length": length}
        )
        _, data = _get_json(f"{HF_ROWS}?{q}")
        batch = (data or {}).get("rows", [])
        if not batch:
            break
        for item in batch:
            row = item.get("row", {})
            rows.append({k: row.get(k) for k in FIELDS})
        offset += length
        if len(batch) < length:
            break
    return rows


def slice_per_lang(rows: list[dict], n: int) -> list[dict]:
    out, seen = [], {}
    for r in rows:
        lang = (r.get("repo_language") or "unknown").lower()
        if seen.get(lang, 0) < n:
            out.append(r)
            seen[lang] = seen.get(lang, 0) + 1
    return out


def check_one(row: dict, hub_tags: dict[str, int]) -> dict:
    tag = row.get("dockerhub_tag") or ""
    exists = tag in hub_tags
    size = hub_tags.get(tag, 0)
    return {
        "instance_id": row.get("instance_id"),
        "repo": row.get("repo"),
        "repo_language": (row.get("repo_language") or "unknown").lower(),
        "dockerhub_tag": tag,
        "image": f"{IMAGE_REPO}:{tag}",
        "exists": exists,
        "full_size": size,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".claude/reports/pro", help="output dir")
    ap.add_argument("--dataset-jsonl", default=None,
                    help="local dataset jsonl (skip HF materialize)")
    ap.add_argument("--per-lang", type=int, default=0,
                    help="if >0, keep only N tasks per repo_language (dev slice)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--stamp", default=None,
                    help="UTC stamp for filenames (caller-supplied; avoids Date.now in-script)")
    args = ap.parse_args()

    stamp = args.stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.dataset_jsonl:
        rows = [json.loads(l) for l in Path(args.dataset_jsonl).read_text(encoding="utf-8").splitlines() if l.strip()]
        rows = [{k: r.get(k) for k in FIELDS} for r in rows]
        print(f"loaded {len(rows)} rows from {args.dataset_jsonl}")
    else:
        print(f"materializing tags from HF {DATASET} [test] ...")
        rows = materialize_tags()
        print(f"materialized {len(rows)} rows")

    if args.per_lang > 0:
        rows = slice_per_lang(rows, args.per_lang)
        print(f"sliced to {len(rows)} rows ({args.per_lang}/language)")

    print("fetching jefzda/sweap-images tag list (paged, once) ...")
    hub_tags = fetch_all_hub_tags()
    print(f"hub has {len(hub_tags)} tags")

    results = [check_one(r, hub_tags) for r in rows]

    results.sort(key=lambda d: (d["repo_language"], d["instance_id"] or ""))
    manifest = out / f"pro_image_manifest_{stamp}.jsonl"
    with manifest.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r) + "\n")

    by_lang: dict[str, dict] = {}
    missing = []
    total_bytes = 0
    for r in results:
        lang = r["repo_language"]
        b = by_lang.setdefault(lang, {"total": 0, "exists": 0, "bytes": 0})
        b["total"] += 1
        if r["exists"]:
            b["exists"] += 1
            b["bytes"] += r["full_size"]
            total_bytes += r["full_size"]
        else:
            missing.append(r["instance_id"])

    summary = {
        "stamp": stamp,
        "dataset": DATASET,
        "image_repo": IMAGE_REPO,
        "queried": len(results),
        "exists": sum(1 for r in results if r["exists"]),
        "missing_count": len(missing),
        "missing_ids": missing,
        "total_pull_gb": round(total_bytes / 1e9, 2),
        "by_language": {
            k: {**v, "gb": round(v["bytes"] / 1e9, 2)} for k, v in sorted(by_lang.items())
        },
        "manifest": str(manifest),
    }
    (out / f"pro_image_preverify_{stamp}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print("\n=== SWE-bench Pro image pre-verify ===")
    print(f"queried={summary['queried']} exists={summary['exists']} "
          f"missing={summary['missing_count']} total_pull={summary['total_pull_gb']} GB")
    for lang, v in summary["by_language"].items():
        print(f"  {lang:8s} {v['exists']}/{v['total']:<4d} images  {v['gb']:.1f} GB")
    print(f"manifest: {manifest}")
    if missing:
        print(f"FATAL: {len(missing)} images MISSING (fail-closed) -> {missing[:5]}{'...' if len(missing)>5 else ''}")
        return 1
    print("PASS: all queried Pro images resolvable on jefzda/sweap-images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
