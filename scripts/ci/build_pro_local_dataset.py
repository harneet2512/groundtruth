#!/usr/bin/env python3
"""Build an offline SWE-bench Pro JSONL for one or more matrix tasks.

The Pro workflow already clones scaleapi/SWE-bench_Pro-os for evaluator scripts.
That repository also carries the full public benchmark rows in
helper_code/sweap_eval_full_v2.jsonl. Use that local file at trial time instead
of reaching out to Hugging Face.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_GOLD_FIELDS = {"patch", "test_patch"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"FATAL: invalid JSON at {path}:{lineno}: {exc}") from exc
            if not isinstance(row, dict):
                raise SystemExit(f"FATAL: non-object JSON row at {path}:{lineno}")
            rows.append(row)
    return rows


def _tag_index(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(path):
        iid = str(row.get("instance_id") or "").strip()
        tag = str(row.get("dockerhub_tag") or "").strip()
        if not iid or not tag:
            raise SystemExit(f"FATAL: Pro tag manifest row missing instance_id/dockerhub_tag: {row}")
        out[iid] = row
    return out


def _selected_ids(instance_ids: str) -> set[str] | None:
    ids = {part.strip() for part in instance_ids.split(",") if part.strip()}
    return ids or None


def build_rows(pro_os_dir: Path, tag_manifest: Path, instance_ids: str = "") -> list[dict[str, Any]]:
    source = pro_os_dir / "helper_code" / "sweap_eval_full_v2.jsonl"
    if not source.is_file():
        raise SystemExit(f"FATAL: offline Pro dataset missing at {source}")
    tags = _tag_index(tag_manifest)
    selected = _selected_ids(instance_ids)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in _read_jsonl(source):
        iid = str(row.get("instance_id") or "").strip()
        if selected is not None and iid not in selected:
            continue
        tag_row = tags.get(iid)
        if tag_row is None:
            continue
        clean = {k: v for k, v in row.items() if k not in _GOLD_FIELDS}
        clean["instance_id"] = iid
        clean["repo_language"] = tag_row.get("repo_language") or clean.get("repo_language") or ""
        clean["dockerhub_tag"] = tag_row["dockerhub_tag"]
        clean["docker_image"] = f"jefzda/sweap-images:{tag_row['dockerhub_tag']}"
        clean["image_name"] = clean["docker_image"]
        rows.append(clean)
        seen.add(iid)

    if selected is not None:
        missing = sorted(selected - seen)
        if missing:
            raise SystemExit(f"FATAL: selected Pro task(s) missing from offline dataset: {missing}")
    if not rows:
        raise SystemExit("FATAL: offline Pro dataset selection is empty")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pro-os-dir", type=Path, default=Path("swebench-pro-os"))
    parser.add_argument("--tag-manifest", type=Path, default=Path("benchmarks/data/swebench_pro_public_tags.jsonl"))
    parser.add_argument("--instance-ids", default="")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    rows = build_rows(args.pro_os_dir, args.tag_manifest, args.instance_ids)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(f"wrote offline Pro dataset: {args.out} rows={len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
