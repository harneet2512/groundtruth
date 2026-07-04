#!/usr/bin/env python3
"""Select a small multilingual target set for industrial/SOTA validation."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

SURFACE_MANIFESTS = {
    "iteration": "artifact_multilingual/repo_manifest.json",
    "deepswe": "artifact_deepswe/repo_manifest.json",
}


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks = data.get("tasks") or []
    if not isinstance(tasks, list):
        raise SystemExit("manifest tasks must be a list")
    return [t for t in tasks if isinstance(t, dict) and t.get("instance_id")]


def select(tasks: list[dict[str, Any]], per_language: int, max_total: int) -> list[dict[str, Any]]:
    by_lang: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        by_lang[str(task.get("language") or "unknown")].append(task)
    selected: list[dict[str, Any]] = []
    for lang in sorted(by_lang):
        pool = sorted(
            by_lang[lang],
            key=lambda t: (
                str(t.get("category") or ""),
                int(t.get("instruction_chars") or 0),
                str(t.get("instance_id")),
            ),
        )
        selected.extend(pool[:per_language])
    if len(selected) < max_total:
        picked = {t["instance_id"] for t in selected}
        rest = sorted(
            [t for t in tasks if t["instance_id"] not in picked],
            key=lambda t: (
                -int(t.get("instruction_chars") or 0),
                str(t.get("language") or ""),
                str(t.get("instance_id")),
            ),
        )
        selected.extend(rest[: max_total - len(selected)])
    return selected[:max_total]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--surface",
        choices=tuple(SURFACE_MANIFESTS),
        default="iteration",
        help="iteration uses the open-source multilingual anti-overfit manifest; deepswe is held-out validation.",
    )
    parser.add_argument("--manifest", help="Override target manifest path")
    parser.add_argument("--per-language", type=int, default=1)
    parser.add_argument("--max-total", type=int, default=8)
    parser.add_argument("--format", choices=("ids", "json"), default="ids")
    parser.add_argument("--json", action="store_true", help="Alias for --format json")
    args = parser.parse_args(argv)
    manifest = args.manifest or SURFACE_MANIFESTS[args.surface]
    tasks = _load_manifest(Path(manifest))
    chosen = select(tasks, args.per_language, args.max_total)
    if args.json or args.format == "json":
        print(json.dumps({"schema": "gt.industrial_sota_targets.v1", "tasks": chosen}, indent=2, sort_keys=True))
    else:
        print(",".join(str(t["instance_id"]) for t in chosen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
