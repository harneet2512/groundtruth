"""Reshape OpenHands ``output.jsonl`` into SWE-bench ``preds.jsonl``.

The SWE-bench-Live/OpenHands fork emits one JSON record per instance. The
microsoft/SWE-bench-Live ``python-only`` evaluator expects records with exactly
three fields per line::

    {"instance_id": ..., "model_name_or_path": ..., "model_patch": ...}

This script is a pure reshape. It does not filter, it does not re-run. If the
fork's output is missing the git patch for an instance, we emit an empty patch
so the evaluator produces an ``empty_patch`` verdict rather than dropping the
instance silently --- silent drops are a health-gate failure (see the plan's
section L Gate 1).

Usage::

    python oh_output_to_preds.py \
        --inputs /path/to/shard_A/output.jsonl /path/to/shard_B/output.jsonl \
        --model-name "vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas" \
        --out /path/to/preds.jsonl

The model name must match what we report to the leaderboard metadata so the
evaluator's per-model directory is named consistently.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PATCH_KEYS = ("model_patch", "git_patch", "test_patch", "patch")


def _extract_patch(record: dict) -> str:
    """Pull the model-produced diff out of an OH record, tolerant of schema drift."""
    for key in PATCH_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value

    # Newer OH forks nest the patch under test_result / git_patch.
    tr = record.get("test_result")
    if isinstance(tr, dict):
        for key in PATCH_KEYS:
            value = tr.get(key)
            if isinstance(value, str) and value.strip():
                return value

    return ""


def _extract_instance_id(record: dict) -> str | None:
    for key in ("instance_id", "id"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def reshape_one(record: dict, model_name: str) -> dict | None:
    iid = _extract_instance_id(record)
    if iid is None:
        return None
    return {
        "instance_id": iid,
        "model_name_or_path": model_name,
        "model_patch": _extract_patch(record),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", nargs="+", required=True, help="One or more OH output.jsonl paths")
    p.add_argument("--out", required=True, help="Destination preds.jsonl path")
    p.add_argument(
        "--model-name",
        default="vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas",
        help="model_name_or_path to emit on every record",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Fail on malformed records instead of skipping",
    )
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    seen: set[str] = set()
    emitted = 0
    empty_patch = 0
    skipped = 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as fout:
        for inp in args.inputs:
            inp_path = Path(inp)
            if not inp_path.exists():
                logger.warning("input missing: %s", inp_path)
                continue
            with inp_path.open("r", encoding="utf-8") as fin:
                for line_no, line in enumerate(fin, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as e:
                        if args.strict:
                            raise
                        logger.warning("skip malformed json %s:%d (%s)", inp_path, line_no, e)
                        skipped += 1
                        continue
                    reshaped = reshape_one(record, args.model_name)
                    if reshaped is None:
                        if args.strict:
                            raise SystemExit(f"missing instance_id at {inp_path}:{line_no}")
                        logger.warning("skip (no instance_id) %s:%d", inp_path, line_no)
                        skipped += 1
                        continue
                    iid = reshaped["instance_id"]
                    if iid in seen:
                        logger.warning("duplicate instance_id %s in %s", iid, inp_path)
                        continue
                    seen.add(iid)
                    if not reshaped["model_patch"]:
                        empty_patch += 1
                    fout.write(json.dumps(reshaped) + "\n")
                    emitted += 1

    logger.info(
        "wrote %s  emitted=%d empty_patch=%d skipped=%d",
        out_path,
        emitted,
        empty_patch,
        skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
