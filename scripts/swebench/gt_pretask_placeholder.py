#!/usr/bin/env python3
"""GT_PRETASK_PATH-compatible script that emits the length-matched neutral
placeholder used for Condition B in the Live Lite paired A/B run.

Mirrors the CLI surface of gt_pretask_brief_v1r.py so the OH GT_PATCH_V3
indexer block can invoke it interchangeably. All flags are accepted; only
the placeholder text is emitted to stdout.

Lookup order for the placeholder text (first non-empty wins):
  1. --placeholder-file <path>
  2. env GT_PLACEHOLDER_FILE
  3. <repo>/benchmarks/live_lite_placeholder.json (sibling-of-script default)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _resolve_placeholder_file(cli_path: str) -> Path:
    if cli_path:
        return Path(cli_path)
    env_path = os.environ.get("GT_PLACEHOLDER_FILE", "").strip()
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parents[2] / "benchmarks" / "live_lite_placeholder.json"


def main() -> int:
    p = argparse.ArgumentParser(description="Live Lite Condition B placeholder emitter")
    # Match gt_pretask_brief_v1r.py CLI surface so the OH harness can swap in.
    p.add_argument("--db", default="")
    p.add_argument("--root", default="/workspace")
    p.add_argument("--issue-text-file", default="")
    p.add_argument("--telemetry-out", default="")
    p.add_argument("--max-files", type=int, default=5)
    p.add_argument("--max-funcs-per-file", type=int, default=3)
    p.add_argument("--task-id", default="unknown")
    p.add_argument("--gold-paths-out", default="")
    p.add_argument("--placeholder-file", default="")
    args, _unknown = p.parse_known_args()

    src = _resolve_placeholder_file(args.placeholder_file)
    if not src.is_file():
        print(f"[gt_pretask_placeholder] FATAL: placeholder file not found at {src}",
              file=sys.stderr)
        return 1

    data = json.loads(src.read_text(encoding="utf-8"))
    text = data.get("text", "")
    if not text:
        print(f"[gt_pretask_placeholder] FATAL: placeholder file has empty 'text' field",
              file=sys.stderr)
        return 1

    sys.stdout.write(text)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
