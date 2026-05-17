"""Compute the 3-arm canary table from archived agent traces.

Each arm is a directory of ``task-*/results/.../output.jsonl`` artifacts. The
script extracts:

- ``first_gold_view_step``       (action index where agent first read a gold file)
- ``first_gold_edit_step``       (action index where agent first edited a gold file)
- ``files_viewed_before_gold``   (distinct files viewed before first gold view)
- ``action_count``               (non-think/recall/message actions)
- ``edit_file_precision``        (|edited basenames ∩ gold basenames| / |edited|)
- ``bridge_event_before_gold``   ([GT] markers referencing a gold file before first read)
- ``agent_followed_gt_edge``     ([GT] suggestion file appears in next 3 reads)
- ``stale_guidance_count``       ([GT] "Next: read X" where X was already viewed)
- ``late_guidance_count``        ([GT] markers AFTER the agent has already edited the file)
- ``injections_per_task``        (total [GT] markers in trajectory)
- ``resolved``                   (from test_result, lagging outcome)

Per-task ``action_economy`` is computed as ``action_count / baseline_action_count``.

The output is a paired CANARY_COMPARISON.md plus a sibling JSON dump for any
follow-on analysis. There is no statistical claim — the canary is for
regression detection, not proof.

Usage:
    python scripts/compute_canary_metrics.py \\
        --baseline .tmp_run_20_baseline \\
        --old-gt   .tmp_diag_artifacts \\
        --v2       <none for now> \\
        --report   reports/canary/CANARY_COMPARISON.md
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Allow running from the repo without "pip install -e .".
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from groundtruth.state.agent_state import canonical_repo_path


_OUT_GLOB = "task-*/results/SWE-bench-Live__SWE-bench-Live-lite/CodeActAgent/*/output.jsonl"
_GT_MARKER = re.compile(r"\[GT[_\] ]|<gt-")
_NEXT_READ_PAT = re.compile(r"Next:\s*read\s+([^\s\n]+)")


def _read_output(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            line = fh.readline()
        if not line:
            return None
        return json.loads(line)
    except (OSError, json.JSONDecodeError):
        return None


def _extract_gold(test_result: dict[str, Any]) -> set[str]:
    gold: set[str] = set()
    patch = test_result.get("git_patch", "") if isinstance(test_result, dict) else ""
    if isinstance(patch, str):
        for line in patch.splitlines():
            if line.startswith("+++ b/"):
                gold.add(line[len("+++ b/"):].strip())
    return gold


def _is_gold(path: str, gold: set[str], repo_root: str = "/workspace") -> bool:
    canon = canonical_repo_path(path, repo_root)
    if not canon:
        return False
    for g in gold:
        if g == canon or g.endswith("/" + canon) or canon.endswith("/" + g):
            return True
        if os.path.basename(g) == os.path.basename(canon):
            return True
    return False


def _action_metrics(history: list[dict[str, Any]], gold: set[str], repo_root: str) -> dict[str, Any]:
    action_count = 0
    first_gold_view: int | None = None
    first_gold_edit: int | None = None
    distinct_files_before_gold: set[str] = set()
    edited_basenames: set[str] = set()
    edited_canonicals: set[str] = set()
    # GT markers
    bridges = 0
    followed_gt = 0
    stale = 0
    late = 0
    injections = 0

    visited_canon: set[str] = set()
    pending_gt_suggestions: list[dict[str, Any]] = []  # iter, target

    for idx, ev in enumerate(history):
        action = ev.get("action") or ""
        args = ev.get("args") or {}
        path = args.get("path") if isinstance(args, dict) else ""

        # action_count
        if action and action not in ("think", "recall", "message"):
            action_count += 1

        is_read = action == "read" and path
        is_edit = (action == "edit" or "str_replace" in str(args.get("command", "") if isinstance(args, dict) else "")) and path

        if is_read:
            canon = canonical_repo_path(path, repo_root)
            if canon:
                if first_gold_view is None and _is_gold(path, gold, repo_root):
                    first_gold_view = action_count
                if first_gold_view is None:
                    distinct_files_before_gold.add(canon)
                visited_canon.add(canon)
            # Check pending GT suggestions for follow-through.
            still_pending: list[dict[str, Any]] = []
            for sug in pending_gt_suggestions:
                if (
                    sug["target"]
                    and (
                        canon == sug["target"]
                        or canon.endswith("/" + sug["target"])
                        or sug["target"].endswith("/" + canon)
                    )
                ):
                    followed_gt += 1
                    continue  # consumed
                if action_count - sug["iter_emitted"] < 3:
                    still_pending.append(sug)
            pending_gt_suggestions = still_pending

        if is_edit:
            canon = canonical_repo_path(path, repo_root)
            if canon:
                if first_gold_edit is None and _is_gold(path, gold, repo_root):
                    first_gold_edit = action_count
                edited_basenames.add(os.path.basename(canon))
                edited_canonicals.add(canon)

        # Look at the next observation text for [GT] markers; classify them.
        next_msg = ""
        if idx + 1 < len(history):
            nxt = history[idx + 1]
            if nxt.get("source") != "agent" or not nxt.get("action"):
                next_msg = nxt.get("message", "") or ""
        if _GT_MARKER.search(next_msg):
            injections += 1
            # Bridge: [GT] referenced a gold file BEFORE the agent first read it.
            for g in gold:
                if g and g in next_msg and first_gold_view is None:
                    bridges += 1
                    break
            # Late: [GT] fired on an edit whose target was already edited.
            if is_edit:
                canon = canonical_repo_path(path, repo_root)
                if canon in edited_canonicals and len(edited_canonicals) > 1:
                    late += 1
            # Stale: "Next: read X" where X was already viewed.
            m = _NEXT_READ_PAT.search(next_msg)
            if m:
                next_target = canonical_repo_path(m.group(1), repo_root)
                if next_target and next_target in visited_canon:
                    stale += 1
                if next_target:
                    pending_gt_suggestions.append({
                        "iter_emitted": action_count,
                        "target": next_target,
                    })

    gold_basenames = {os.path.basename(g) for g in gold}
    edit_precision = (
        len(edited_basenames & gold_basenames) / len(edited_basenames)
        if edited_basenames
        else 0.0
    )
    return {
        "action_count": action_count,
        "first_gold_view_step": first_gold_view,
        "first_gold_edit_step": first_gold_edit,
        "files_viewed_before_gold": len(distinct_files_before_gold),
        "edit_file_precision": edit_precision,
        "bridge_event_before_gold": bridges,
        "agent_followed_gt_edge": followed_gt,
        "stale_guidance_count": stale,
        "late_guidance_count": late,
        "injections_per_task": injections,
    }


def _resolve_resolved(data: dict[str, Any]) -> bool:
    tr = data.get("test_result") or {}
    if isinstance(tr, dict):
        if "resolved" in tr:
            return bool(tr["resolved"])
        # SWE-bench-Live shape: tests_status.FAIL_TO_PASS.success / failure
        ts = tr.get("tests_status") or {}
        f2p = ts.get("FAIL_TO_PASS") or {}
        succ = f2p.get("success") or []
        fail = f2p.get("failure") or []
        if succ and not fail:
            return True
    return False


def _arm_task_map(arm_dir: str) -> dict[str, Path]:
    """Map task_id -> path to output.jsonl inside ``arm_dir``."""
    out: dict[str, Path] = {}
    if not arm_dir or not os.path.isdir(arm_dir):
        return out
    for p in sorted(glob.glob(os.path.join(arm_dir, _OUT_GLOB))):
        path = Path(p)
        # task_id is the parent task-* dir.
        parts = path.parts
        for part in parts:
            if part.startswith("task-"):
                tid = part[len("task-"):]
                out[tid] = path
                break
    return out


def _arm_metrics(arm_dir: str, repo_root: str) -> dict[str, dict[str, Any]]:
    by_task: dict[str, dict[str, Any]] = {}
    for tid, path in _arm_task_map(arm_dir).items():
        data = _read_output(path)
        if not data:
            continue
        history = data.get("history") or []
        gold = _extract_gold(data.get("test_result") or {})
        m = _action_metrics(history, gold, repo_root)
        m["resolved"] = _resolve_resolved(data)
        m["gold_files"] = sorted(gold)
        m["output_path"] = str(path)
        by_task[tid] = m
    return by_task


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "Y" if v else "N"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _row(metric: str, values: dict[str, Any]) -> str:
    return f"| {metric} | " + " | ".join(_fmt(values.get(k)) for k in ("baseline", "old_gt", "v2")) + " |"


def _build_markdown(
    baseline_dir: str,
    old_gt_dir: str,
    v2_dir: str,
    baseline: dict[str, dict[str, Any]],
    old_gt: dict[str, dict[str, Any]],
    v2: dict[str, dict[str, Any]],
) -> str:
    shared_tasks = sorted(set(baseline) & set(old_gt))
    v2_avail = bool(v2)
    if v2_avail:
        shared_tasks = sorted(set(shared_tasks) & set(v2))

    lines: list[str] = []
    lines.append("# CANARY_COMPARISON — 3-arm paired metric table")
    lines.append("")
    lines.append("Status: regression-detection canary. **Not a success claim.**")
    lines.append("")
    lines.append("## Arms")
    lines.append(f"- BASELINE     : `{baseline_dir or '—'}` ({len(baseline)} tasks)")
    lines.append(f"- OLD_GT       : `{old_gt_dir or '—'}` ({len(old_gt)} tasks)")
    lines.append(f"- V2_ROUTER_GT : `{v2_dir or 'pending VM run'}` ({len(v2)} tasks)")
    lines.append(f"- shared tasks across all populated arms: {len(shared_tasks)}")
    lines.append("")
    if not v2_avail:
        lines.append("> **V2_ROUTER_GT is missing.** The wrapper carries `GT_ROUTER_V2=1` but no run has been executed yet. See `docs/handoff/canary_v2_runbook.md` for the launch command. This file records BASELINE vs OLD_GT for regression detection in the meantime.")
        lines.append("")

    lines.append("## Per-task table")
    lines.append("")
    if not shared_tasks:
        lines.append("_No tasks overlap across all populated arms._")
        return "\n".join(lines) + "\n"

    for tid in shared_tasks:
        lines.append(f"### {tid}")
        bl = baseline.get(tid, {})
        og = old_gt.get(tid, {})
        v2t = v2.get(tid, {}) if v2_avail else {}
        lines.append("")
        lines.append("| metric | baseline | OLD_GT | V2_ROUTER_GT |")
        lines.append("|--------|----------|--------|---------------|")
        for metric in (
            "first_gold_view_step",
            "first_gold_edit_step",
            "files_viewed_before_gold",
            "action_count",
            "edit_file_precision",
            "bridge_event_before_gold",
            "agent_followed_gt_edge",
            "stale_guidance_count",
            "late_guidance_count",
            "injections_per_task",
            "resolved",
        ):
            lines.append(_row(metric, {"baseline": bl.get(metric), "old_gt": og.get(metric), "v2": v2t.get(metric) if v2_avail else None}))
        # action_economy per task.
        bl_actions = bl.get("action_count") or 0
        og_ratio = (og.get("action_count") / bl_actions) if bl_actions else None
        v2_ratio = (v2t.get("action_count") / bl_actions) if (v2_avail and bl_actions) else None
        lines.append(_row("action_economy (GT/BL)", {"baseline": 1.0, "old_gt": og_ratio, "v2": v2_ratio}))
        # Note any gold-file mismatch between arms.
        if bl.get("gold_files") != og.get("gold_files"):
            lines.append(f"\n*Note: gold files differ across arms — baseline `{bl.get('gold_files')}` vs OLD_GT `{og.get('gold_files')}`*")
        lines.append("")

    # Aggregate medians.
    def _median(arm: dict[str, dict[str, Any]], key: str) -> float | None:
        vals = [arm[t].get(key) for t in shared_tasks if arm.get(t, {}).get(key) is not None]
        return statistics.median(vals) if vals else None

    lines.append("## Aggregate medians (over shared tasks)")
    lines.append("")
    lines.append("| metric | baseline | OLD_GT | V2_ROUTER_GT |")
    lines.append("|--------|----------|--------|---------------|")
    for metric in (
        "first_gold_view_step",
        "files_viewed_before_gold",
        "action_count",
        "edit_file_precision",
        "injections_per_task",
        "stale_guidance_count",
        "late_guidance_count",
    ):
        lines.append(_row(metric, {
            "baseline": _median(baseline, metric),
            "old_gt": _median(old_gt, metric),
            "v2": _median(v2, metric) if v2_avail else None,
        }))
    # Aggregate resolved counts.
    bl_resolved = sum(1 for t in shared_tasks if baseline.get(t, {}).get("resolved"))
    og_resolved = sum(1 for t in shared_tasks if old_gt.get(t, {}).get("resolved"))
    v2_resolved = sum(1 for t in shared_tasks if v2.get(t, {}).get("resolved")) if v2_avail else None
    lines.append(_row(
        f"resolved (of {len(shared_tasks)})",
        {"baseline": bl_resolved, "old_gt": og_resolved, "v2": v2_resolved},
    ))
    lines.append("")
    lines.append("## Decision rule (per session directive)")
    lines.append("")
    lines.append("- If V2 is worse than OLD_GT on action-path metrics: do NOT continue V2 activation.")
    lines.append("- If V2 matches OLD_GT with fewer stale/late/injection events: continue to 5-task paired holdout.")
    lines.append("- If V2 beats OLD_GT and BASELINE on action-path metrics: this is still a canary pass, not a success.")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- All metrics are descriptive. The canary is a regression-detection gate, not an evaluation.")
    lines.append("- Resolve is a lagging outcome; do not use it as a single signal.")
    lines.append("- `bridge_event_before_gold`, `agent_followed_gt_edge`, `stale_guidance_count`, `late_guidance_count`, `injections_per_task` are derived from `[GT]` markers in the trajectory observation messages — this matches the live wrapper's evidence-injection format.")
    lines.append("- BASELINE traces should have these GT-derived metrics at 0; non-zero indicates a leak.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", default="", help="Directory of BASELINE task-*/.../output.jsonl artifacts.")
    parser.add_argument("--old-gt", default="", help="Directory of OLD_GT task-*/.../output.jsonl artifacts.")
    parser.add_argument("--v2", default="", help="Directory of V2_ROUTER_GT task-*/.../output.jsonl artifacts.")
    parser.add_argument("--repo-root", default="/workspace", help="Prefix to strip from paths in canonicalization.")
    parser.add_argument("--report", default="reports/canary/CANARY_COMPARISON.md")
    parser.add_argument("--json", default="reports/canary/canary_metrics.json")
    args = parser.parse_args()

    baseline = _arm_metrics(args.baseline, args.repo_root) if args.baseline else {}
    old_gt = _arm_metrics(args.old_gt, args.repo_root) if args.old_gt else {}
    v2 = _arm_metrics(args.v2, args.repo_root) if args.v2 else {}

    md_path = Path(args.report)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        _build_markdown(args.baseline, args.old_gt, args.v2, baseline, old_gt, v2),
        encoding="utf-8",
    )
    json_path = Path(args.json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "baseline_dir": args.baseline,
                "old_gt_dir": args.old_gt,
                "v2_dir": args.v2,
                "tasks": {
                    "baseline": baseline,
                    "old_gt": old_gt,
                    "v2": v2,
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(f"Canary table written to {md_path}")
    print(f"Canary metrics JSON written to {json_path}")
    print(
        f"arms populated: baseline={len(baseline)} old_gt={len(old_gt)} v2={len(v2)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
