#!/usr/bin/env python3
"""Assemble a SWE-bench-Live predictions.jsonl from Live Lite inference artifacts.

This is the BRIDGE between the inference workflow (swebench_live_lite_full.yml,
which emits one `ll-full-<task>` artifact per task) and the OFFICIAL evaluator
(live_lite_eval.yml -> `python -m swebench.harness.run_evaluation`). Without it
there is no clean predictions.jsonl for the official harness to grade — the exact
gap that made the SWE-Pro run unscoreable.

Patch source priority (per task), fail-loud on corruption:
  1. agent_patch.diff     — bind-mount raw /tmp/patch.txt (uncorrupted). PRIMARY.
  2. git_diff_head.diff   — bind-mount `git diff HEAD` from the container.
  3. trial_output.log     — extract + unwrap terminal-wrapping (lossy FALLBACK).
  4. none                 — empty model_patch (task still counted in denominator).

Output record (SWE-bench standard, matches live_lite_inference.yml):
  {"instance_id": ..., "model_patch": ..., "model_name_or_path": ...}

Usage:
  python build_ll_predictions.py <artifacts_dir> <out_predictions.jsonl> \
      [--expected <dataset.jsonl>] [--model "<label>"]

--expected pins the denominator: every instance_id in the dataset gets a record
(empty patch if the task produced none), so the official resolved-rate is over
the FULL task set, never silently narrowed to the tasks that happened to emit a
patch. Missing tasks are reported, not dropped.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

# Import the sibling unwrapper (importable regardless of CWD).
_UNWRAP_PATH = Path(__file__).resolve().parent / "unwrap_patch.py"
_spec = importlib.util.spec_from_file_location("unwrap_patch_mod", _UNWRAP_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load unwrap_patch from {_UNWRAP_PATH}")
_uw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_uw)

_DEFAULT_MODEL = "GroundTruth + mini-swe-agent + deepseek-v4-flash"


def _patch_for_task(task_dir: Path) -> tuple[str, str]:
    """Return (patch_text, source) for one ll-full-<task> artifact directory.

    source is one of: bindmount_agent, bindmount_gitdiff, log_unwrapped, none.
    """
    agent = task_dir / "agent_patch.diff"
    if agent.is_file() and agent.stat().st_size > 0:
        txt = agent.read_text(encoding="utf-8", errors="replace")
        # bind-mount capture is raw; only normalize CRLF the harness would choke on.
        if _looks_like_diff(txt):
            return txt.replace("\r\n", "\n"), "bindmount_agent"

    gitdiff = task_dir / "git_diff_head.diff"
    if gitdiff.is_file() and gitdiff.stat().st_size > 0:
        txt = gitdiff.read_text(encoding="utf-8", errors="replace")
        if _looks_like_diff(txt):
            return txt.replace("\r\n", "\n"), "bindmount_gitdiff"

    log = task_dir / "trial_output.log"
    if log.is_file():
        raw = _uw.extract_patch_from_log(
            log.read_text(encoding="utf-8", errors="replace")
        )
        if raw:
            fixed, _ = _uw.unwrap_patch(raw)
            if _looks_like_diff(fixed):
                return fixed, "log_unwrapped"

    return "", "none"


def _looks_like_diff(text: str) -> bool:
    """A usable patch must contain at least one file header."""
    return "diff --git " in text or text.lstrip().startswith("--- ")


def _task_id_from_dir(d: Path) -> str:
    name = d.name
    return name[len("ll-full-"):] if name.startswith("ll-full-") else name


def build(artifacts_dir: Path, expected: list[str] | None, model: str):
    dirs = sorted(p for p in artifacts_dir.glob("ll-full-*") if p.is_dir())
    found: dict[str, tuple[str, str]] = {}
    for d in dirs:
        tid = _task_id_from_dir(d)
        found[tid] = _patch_for_task(d)

    # The denominator: the expected set if given, else exactly what we found.
    ids = list(expected) if expected else sorted(found)

    records = []
    src_counts = {"bindmount_agent": 0, "bindmount_gitdiff": 0,
                  "log_unwrapped": 0, "none": 0, "absent_artifact": 0}
    for tid in ids:
        if tid in found:
            patch, src = found[tid]
        else:
            patch, src = "", "absent_artifact"
        src_counts[src] += 1
        records.append({
            "instance_id": tid,
            "model_patch": patch,
            "model_name_or_path": model,
        })

    # Report any artifacts we found that are NOT in the expected set (drift check).
    extra = sorted(set(found) - set(ids)) if expected else []
    return records, src_counts, extra


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("artifacts_dir")
    ap.add_argument("out_path")
    ap.add_argument("--expected", default="",
                    help="dataset .jsonl to pin the full denominator")
    ap.add_argument("--model", default=_DEFAULT_MODEL)
    args = ap.parse_args()

    artifacts_dir = Path(args.artifacts_dir)
    if not artifacts_dir.is_dir():
        print(f"ERROR: artifacts_dir not found: {artifacts_dir}", file=sys.stderr)
        return 2

    expected = None
    if args.expected:
        expected = []
        for line in Path(args.expected).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                expected.append(json.loads(line)["instance_id"])

    records, src_counts, extra = build(artifacts_dir, expected, args.model)

    out = Path(args.out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    with_patch = sum(1 for r in records if r["model_patch"].strip())
    print("=" * 56)
    print("BUILD predictions.jsonl")
    print("=" * 56)
    print(f"  output:            {out}")
    print(f"  records:           {len(records)}")
    print(f"  with patch:        {with_patch}")
    print(f"  empty patch:       {len(records) - with_patch}")
    print(f"  source breakdown:")
    for k in ("bindmount_agent", "bindmount_gitdiff", "log_unwrapped",
              "none", "absent_artifact"):
        print(f"    {k:18s} {src_counts[k]}")
    if extra:
        print(f"  ::warning:: {len(extra)} artifact(s) NOT in expected set: "
              + ", ".join(extra[:10]) + (" ..." if len(extra) > 10 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
