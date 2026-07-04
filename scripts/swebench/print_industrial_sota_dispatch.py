#!/usr/bin/env python3
"""Print paired workflow-dispatch commands for multilingual validation."""
from __future__ import annotations

import argparse
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from select_industrial_sota_targets import SURFACE_MANIFESTS, _load_manifest, select


def _git_ref() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return out or "gt-pro"
    except Exception:
        return "gt-pro"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--surface",
        choices=tuple(SURFACE_MANIFESTS),
        default="iteration",
        help="iteration uses open-source multilingual targets; deepswe is held out for final validation.",
    )
    parser.add_argument("--manifest", help="Override target manifest path")
    parser.add_argument("--per-language", type=int, default=1)
    parser.add_argument("--max-total", type=int, default=8)
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash")
    parser.add_argument("--step-limit", default="80")
    parser.add_argument("--eval-cpus", default="2")
    parser.add_argument("--max-parallel", default="4")
    parser.add_argument("--ref", default=_git_ref())
    parser.add_argument("--workflow", default="deepswe_full.yml")
    parser.add_argument("--gt-substrate-digest", default="${GT_SUBSTRATE_DIGEST}")
    parser.add_argument(
        "--split-by-language",
        action="store_true",
        help="Emit one paired GT/baseline workflow dispatch per selected language.",
    )
    args = parser.parse_args(argv)
    manifest = args.manifest or SURFACE_MANIFESTS[args.surface]
    if args.surface == "iteration" and args.workflow == "deepswe_full.yml" and not args.manifest:
        raise SystemExit(
            "Refusing to dispatch open-source iteration IDs to deepswe_full.yml: "
            "that workflow is hard-wired to artifact_deepswe/deepswe-bench. "
            "Use this selector to choose artifact_multilingual targets for a SWE-bench "
            "Multilingual-capable runner, or pass --surface deepswe for held-out validation."
        )
    tasks = select(_load_manifest(Path(manifest)), args.per_language, args.max_total)
    groups = _language_groups(tasks) if args.split_by_language else {"all": tasks}
    for language, group in groups.items():
        ids = ",".join(str(t["instance_id"]) for t in group)
        for arm in ("gt", "baseline"):
            cmd = _workflow_cmd(args, ids=ids, arm=arm)
            if args.split_by_language:
                print(f"# language={language} arm={arm} tasks={len(group)}")
            print(" ".join(_quote(part) for part in cmd))
    return 0


def _language_groups(tasks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        grouped[str(task.get("language") or "unknown")].append(task)
    return {lang: grouped[lang] for lang in sorted(grouped)}


def _workflow_cmd(args: argparse.Namespace, *, ids: str, arm: str) -> list[str]:
    common = [
        "gh", "workflow", "run", args.workflow,
        "--ref", args.ref,
        "-f", f"model={args.model}",
        "-f", "language=all",
        "-f", "max_tasks=0",
        "-f", f"instance_ids={ids}",
        "-f", f"max_parallel={args.max_parallel}",
        "-f", f"step_limit={args.step_limit}",
        "-f", f"eval_cpus={args.eval_cpus}",
        "-f", f"gt_substrate_digest={args.gt_substrate_digest}",
        "-f", "require_pinned_substrate=1",
    ]
    return common + ["-f", f"gt_arm={arm}"]


def _quote(value: str) -> str:
    if not value or any(ch.isspace() for ch in value) or any(ch in value for ch in "\"'"):
        return "'" + value.replace("'", "'\"'\"'") + "'"
    return value


if __name__ == "__main__":
    raise SystemExit(main())
