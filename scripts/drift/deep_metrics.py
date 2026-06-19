#!/usr/bin/env python3
"""Per-task DEEP metrics for the contract-DRIFT lever (8-decimal), per CLAUDE.md.

Reads a task's `output.jsonl` (the OH history) + `eval_result.json` and emits
`gt_deep_metrics_<task>.json` capturing — for the drift layer specifically — what the
agent ACTUALLY saw and did (AGENT-OBSERVATION rule: raw delivered text, never telemetry):

  drift: eligible / emitted / rendered_tokens_total / raw blocks / utilization_score
  agent: action_count / edit_count / first_edit_action / edited_files / history_events
  outcome: resolved / has_patch / flip / regression (vs frozen baseline)

Schema matches scripts/metrics/compute_run_metrics.py (history = list of events;
actions = events whose `action` is not a non-action; edits = action in edit/write or
'str_replace' in args). All floats rounded to 8 dp.

Usage:
  python scripts/drift/deep_metrics.py --artifacts <dir> [--baseline <FINAL_resolved.json>]
  python scripts/drift/deep_metrics.py --output-jsonl <path> --task <id> --out <file.json>
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from pathlib import Path

_NON_ACTIONS = {"think", "recall", "message", "null", "", None}
_DRIFT_RE = re.compile(r"<gt-drift>(.*?)</gt-drift>", re.DOTALL)
_HEAD_RE = re.compile(r"^\s*(\S+)\s*::\s*(\S+)", re.MULTILINE)


def _r8(x: float) -> float:
    return round(float(x), 8)


def _event_text(e: dict) -> str:
    """All string content of a history event, for substring/symbol search."""
    parts: list[str] = []
    for k in ("content", "message", "observation", "thought"):
        v = e.get(k)
        if isinstance(v, str):
            parts.append(v)
    args = e.get("args")
    if isinstance(args, dict):
        parts.extend(str(v) for v in args.values() if isinstance(v, str))
    elif isinstance(args, str):
        parts.append(args)
    extras = e.get("extras")
    if isinstance(extras, dict):
        parts.extend(str(v) for v in extras.values() if isinstance(v, str))
    return "\n".join(parts)


def _is_action(e: dict) -> bool:
    a = e.get("action")
    return bool(a) and a not in _NON_ACTIONS


def _is_edit(e: dict) -> bool:
    return e.get("action") in ("edit", "write") or "str_replace" in str(e.get("args", {}))


def _edited_path(e: dict) -> str:
    args = e.get("args")
    if isinstance(args, dict):
        for k in ("path", "file", "file_path", "filename"):
            v = args.get(k)
            if isinstance(v, str) and v:
                return v.replace("\\", "/")
    return ""


def compute_drift_metrics(
    history: list[dict],
    *,
    resolved: str,
    patch: str,
    task: str,
    baseline_ids: set[str] | None = None,
) -> dict:
    """The 8-dp deep record for one task. Pure; testable without files."""
    # Locate drift observations + the symbols they named.
    drift_indices: list[int] = []
    drift_blocks: list[str] = []
    drift_symbols: set[str] = set()
    for i, e in enumerate(history):
        txt = _event_text(e)
        for m in _DRIFT_RE.findall(txt):
            drift_indices.append(i)
            drift_blocks.append("<gt-drift>" + m + "</gt-drift>")
            for fpath, fname in _HEAD_RE.findall(m):
                drift_symbols.add(fname)
                drift_symbols.add(os.path.basename(fpath))

    actions = [i for i, e in enumerate(history) if _is_action(e)]
    edit_idx = [i for i, e in enumerate(history) if _is_edit(e)]
    edited_files = sorted({p for e in history if _is_edit(e) and (p := _edited_path(e))})

    # first_edit_action = ordinal (1-based) of the first edit among actions; 0 if none.
    first_edit_action = 0
    for ordinal, i in enumerate(actions, start=1):
        if i in set(edit_idx):
            first_edit_action = ordinal
            break

    emitted = len(drift_indices)
    rendered_tokens_total = sum(max(1, len(b) // 4) for b in drift_blocks)  # ~4 chars/token

    # Utilization (deterministic proxy, AGENT-OBSERVATION based):
    #   +0.5 the agent EDITED after seeing drift (reacted at all)
    #   +0.5 a drift-named symbol/file appears in a later agent action/message (engaged)
    util = 0.0
    if emitted:
        last_drift = max(drift_indices)
        if any(i > last_drift for i in edit_idx):
            util += 0.5
        post_text = "\n".join(_event_text(history[i]) for i in range(last_drift + 1, len(history)))
        if drift_symbols and any(sym and sym in post_text for sym in drift_symbols):
            util += 0.5

    is_resolved = resolved == "RESOLVED"
    flip = bool(is_resolved and baseline_ids is not None and task not in baseline_ids)
    regression = bool((not is_resolved) and baseline_ids is not None and task in baseline_ids)

    return {
        "task": task,
        "drift": {
            "eligible": _r8(1.0 if edited_files else 0.0),
            "emitted": emitted,
            "rendered_tokens_total": rendered_tokens_total,
            "utilization_score": _r8(util),
            "raw_blocks": drift_blocks,  # the RAW delivered text (truth)
            "named_symbols": sorted(drift_symbols),
        },
        "agent": {
            "history_events": len(history),
            "action_count": len(actions),
            "edit_count": len(edit_idx),
            "first_edit_action": first_edit_action,
            "edited_files": edited_files,
        },
        "outcome": {
            "resolved": resolved,
            "has_patch": "diff" in (patch or ""),
            "flip": flip,
            "regression": regression,
        },
    }


def _load_output_jsonl(path: str) -> tuple[list[dict], str]:
    with open(path, encoding="utf-8", errors="replace") as f:
        d = json.loads(f.readline() or "{}")
    patch = (d.get("test_result", {}) or {}).get("git_patch", "") or d.get("git_patch", "") or ""
    return d.get("history", []), patch


def _resolved(task_dir: str) -> str:
    er = glob.glob(os.path.join(task_dir, "**", "eval_result.json"), recursive=True)
    if not er:
        return "unknown"
    try:
        r = json.load(open(er[0], encoding="utf-8"))
    except Exception:
        return "unknown"
    if r.get("status") == "eval_no_report":
        return "no_report"
    if r.get("resolved_instances") == 1 or r.get("resolved_ids"):
        return "RESOLVED"
    return "NO"


def _baseline_ids(path: str) -> set[str]:
    if not path or not os.path.exists(path):
        return set()
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return set()
    return set(d.get("resolved_ids", []) or [])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="contract-drift deep metrics (8dp)")
    ap.add_argument("--artifacts", default="", help="dir of task-<id>/ subtrees")
    ap.add_argument("--output-jsonl", default="", help="single task output.jsonl")
    ap.add_argument("--task", default="", help="task id (single mode)")
    ap.add_argument("--out", default="", help="output json (single mode)")
    ap.add_argument("--baseline", default="", help="frozen baseline FINAL_resolved_*.json")
    a = ap.parse_args(argv)
    base = _baseline_ids(a.baseline)

    if a.output_jsonl:
        hist, patch = _load_output_jsonl(a.output_jsonl)
        rec = compute_drift_metrics(hist, resolved="unknown", patch=patch,
                                    task=a.task or "task", baseline_ids=base or None)
        out = a.out or f"gt_deep_metrics_{a.task or 'task'}.json"
        Path(out).write_text(json.dumps(rec, indent=2), encoding="utf-8")
        print(f"wrote {out} (emitted={rec['drift']['emitted']} util={rec['drift']['utilization_score']})")
        return 0

    if not a.artifacts:
        ap.error("need --artifacts or --output-jsonl")
    task_dirs = sorted(d for d in glob.glob(os.path.join(a.artifacts, "task-*")) if os.path.isdir(d))
    if not task_dirs:
        task_dirs = sorted(d for d in glob.glob(os.path.join(a.artifacts, "*")) if os.path.isdir(d))
    n = 0
    for td in task_dirs:
        ojs = glob.glob(os.path.join(td, "**", "output.jsonl"), recursive=True)
        if not ojs:
            continue
        task = os.path.basename(td).replace("task-", "")
        hist, patch = _load_output_jsonl(ojs[0])
        rec = compute_drift_metrics(hist, resolved=_resolved(td), patch=patch,
                                    task=task, baseline_ids=base or None)
        out = os.path.join(td, f"gt_deep_metrics_{task}.json")
        Path(out).write_text(json.dumps(rec, indent=2), encoding="utf-8")
        n += 1
        print(f"[{task}] emitted={rec['drift']['emitted']} util={rec['drift']['utilization_score']} "
              f"resolved={rec['outcome']['resolved']} flip={rec['outcome']['flip']}")
    print(f"wrote {n} deep-metric records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
