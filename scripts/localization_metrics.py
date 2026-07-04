"""Localization metrics harness — measures GT-agent collaboration quality.

Computes per-task metrics from output.jsonl artifacts:
- L1 ranking quality (hit@K, MRR)
- Runtime navigation quality (L3b bridge events, stale/late guidance)
- Whole localization path efficiency (first_gold_view, action economy)
- Fix quality outcome (resolved, fix_rate) — reported separately

Usage:
    python scripts/localization_metrics.py --gt-dir /tmp/gt_artifacts --bl-dir /tmp/bl_artifacts
"""

import json
import glob
import os
import sys
import argparse
from pathlib import Path


def extract_gold_files_from_patch(data: dict) -> list[str]:
    """Extract gold files from the resolving patch (or attempted patch)."""
    patch = data.get("test_result", {}).get("git_patch", "")
    if not patch:
        return []
    files = []
    for line in patch.split("\n"):
        if line.startswith("+++ b/"):
            f = line[6:].strip()
            if not f.startswith(".openhands") and not f.endswith("TASKS.md"):
                files.append(f)
    return files


def compute_task_metrics(output_jsonl_path: str, task_id: str, gold_files: list[str] | None = None) -> dict:
    """Compute all localization metrics for a single task."""
    with open(output_jsonl_path, encoding="utf-8", errors="replace") as f:
        data = json.loads(f.readline())

    history = data.get("history", [])

    # Extract gold files from patch if not provided
    if gold_files is None:
        gold_files = extract_gold_files_from_patch(data)

    # Normalize gold files
    gold_basenames = {os.path.basename(f) for f in gold_files}
    gold_set = set(gold_files)

    # L1 brief files (from first history entry content)
    l1_files = []
    for e in history[:3]:
        content = str(e.get("content", "") or e.get("args", {}).get("content", "") if isinstance(e.get("args"), dict) else "")
        if "gt-task-brief" in content:
            for line in content.split("\n"):
                line = line.strip()
                if line and line[0:1].isdigit() and ". " in line:
                    fp = line.split(". ", 1)[1].split(" (")[0].strip()
                    l1_files.append(fp)
            break

    # Hit@K
    l1_hit_1 = any(os.path.basename(f) in gold_basenames or f in gold_set for f in l1_files[:1])
    l1_hit_3 = any(os.path.basename(f) in gold_basenames or f in gold_set for f in l1_files[:3])
    l1_hit_5 = any(os.path.basename(f) in gold_basenames or f in gold_set for f in l1_files[:5])

    # MRR
    mrr = 0.0
    for i, f in enumerate(l1_files):
        if os.path.basename(f) in gold_basenames or f in gold_set:
            mrr = 1.0 / (i + 1)
            break

    # Trajectory analysis
    actions = []
    files_viewed = []
    files_edited = []
    gt_events = []
    first_gold_view = None
    first_gold_edit = None
    first_edit = None

    for i, e in enumerate(history):
        action = e.get("action", "")
        content = str(e.get("content", ""))
        args = e.get("args", {}) if isinstance(e.get("args"), dict) else {}
        path = args.get("path", "")

        if action and action not in ("think", "recall", "message"):
            actions.append(i)

        # Track file views
        if action == "read" and path:
            basename = os.path.basename(path)
            files_viewed.append((i, path, basename))
            if first_gold_view is None and (basename in gold_basenames or any(g in path for g in gold_files)):
                first_gold_view = len(actions)

        # Track file edits
        if action in ("edit", "write") or "str_replace" in str(args):
            if path and "TASKS" not in path and "scaffold" not in path:
                basename = os.path.basename(path)
                files_edited.append((i, path, basename))
                if first_edit is None:
                    first_edit = len(actions)
                if first_gold_edit is None and (basename in gold_basenames or any(g in path for g in gold_files)):
                    first_gold_edit = len(actions)

        # Track GT evidence
        if "[GT]" in content:
            gt_events.append((i, content))

    # GT event classification: stale, bridge, late
    # Bridge = GT shows caller/callee/connection pointing to gold (not just "Next: read")
    # Stale = GT gives "Next: read X" where X already viewed
    # Late = GT evidence about gold AFTER agent already edited gold
    l3b_bridges = 0
    stale_count = 0
    late_count = 0
    gt_all_events = 0
    already_viewed_paths = set()
    gold_already_edited = False
    unique_files_before_gold = set()
    _found_gold_view = False

    for i, e in enumerate(history):
        content = str(e.get("content", ""))
        action = e.get("action", "")
        args = e.get("args", {}) if isinstance(e.get("args"), dict) else {}
        path = args.get("path", "")

        if action == "read" and path:
            rel = path.split("/workspace/")[-1] if "/workspace/" in path else path
            already_viewed_paths.add(rel)
            if not _found_gold_view:
                bn = os.path.basename(rel)
                if bn in gold_basenames or any(g in rel for g in gold_files):
                    _found_gold_view = True
                else:
                    unique_files_before_gold.add(rel)

        if action in ("edit", "write") or "str_replace" in str(args):
            if path:
                rel_e = path.split("/workspace/")[-1] if "/workspace/" in path else path
                if os.path.basename(rel_e) in gold_basenames:
                    gold_already_edited = True

        if "[GT]" in content or "[GT-router-v2" in content or "[GT L5" in content or ("gt-task-brief" in content and i < 5):
            gt_all_events += 1

            if gold_already_edited and any(g in content for g in gold_files):
                late_count += 1
                continue

            if "Next: read" in content:
                next_part = content.split("Next: read")[-1].strip().split("\n")[0].strip()
                suggested_rel = next_part.split("/workspace/")[-1] if "/workspace/" in next_part else next_part
                if suggested_rel in already_viewed_paths:
                    stale_count += 1
                elif any(suggested_rel in vp or vp.endswith(suggested_rel) for vp in already_viewed_paths):
                    stale_count += 1
                elif any(suggested_rel.endswith(g) or g in suggested_rel for g in gold_files):
                    l3b_bridges += 1
            elif any(g in content for g in gold_files):
                l3b_bridges += 1

    # Edit file precision
    edit_basenames = {bn for _, _, bn in files_edited}
    if edit_basenames:
        edit_precision = len(edit_basenames & gold_basenames) / len(edit_basenames)
    else:
        edit_precision = 0.0

    # Resolve + fix rate
    resolved = False
    fix_rate = 0.0
    try:
        eval_dir = os.path.dirname(os.path.dirname(output_jsonl_path))
        eval_path = os.path.join(eval_dir, "..", "..", "eval_result.json")
        # Try multiple paths
        for candidate in [eval_path, os.path.join(os.path.dirname(output_jsonl_path), "..", "..", "..", "eval_result.json")]:
            if os.path.exists(candidate):
                r = json.load(open(candidate, encoding="utf-8"))
                for k, v in r.items():
                    if isinstance(v, dict):
                        resolved = v.get("resolved", False)
                        f2p = v.get("tests_status", {}).get("FAIL_TO_PASS", {})
                        p2p = v.get("tests_status", {}).get("PASS_TO_PASS", {})
                        f2p_s = len(f2p.get("success", []))
                        f2p_t = f2p_s + len(f2p.get("failure", []))
                        p2p_f = len(p2p.get("failure", []))
                        fix_rate = 0.0 if p2p_f > 0 else (f2p_s / f2p_t if f2p_t > 0 else 0.0)
                        break
                break
    except Exception:
        pass

    return {
        "task_id": task_id,
        "l1_brief_files": l1_files,
        "gold_files": gold_files,
        "l1_hit_1": l1_hit_1,
        "l1_hit_3": l1_hit_3,
        "l1_hit_5": l1_hit_5,
        "l1_mrr": mrr,
        "first_gold_view_step": first_gold_view,
        "first_gold_edit_step": first_gold_edit,
        "first_edit_step": first_edit,
        "files_viewed_before_gold": len(unique_files_before_gold),
        "total_files_viewed": len(files_viewed),
        "action_count": len(actions),
        "edit_count": len(files_edited),
        "edit_file_precision": edit_precision,
        "gt_all_events": gt_all_events,
        "l3b_bridge_events": l3b_bridges,
        "stale_guidance_count": stale_count,
        "late_guidance_count": late_count,
        "resolved": resolved,
        "fix_rate": fix_rate,
    }


def print_metrics_table(metrics_list: list[dict], label: str = "") -> None:
    """Print metrics as a readable table."""
    if label:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")

    print(f"\n{'Task':<20} {'hit@5':>5} {'MRR':>5} {'1st_gold':>8} {'files_b4':>8} {'actions':>7} {'edit_prec':>9} {'bridges':>7} {'stale':>5} {'late':>4} {'resolved':>8}")
    print("-" * 110)
    for m in metrics_list:
        fgv = str(m['first_gold_view_step'] or '-')
        fb4 = str(m['files_viewed_before_gold'])
        print(f"{m['task_id']:<20} {int(m['l1_hit_5']):>5} {m['l1_mrr']:>5.2f} {fgv:>8} {fb4:>8} {m['action_count']:>7} {m['edit_file_precision']:>9.2f} {m['l3b_bridge_events']:>7} {m['stale_guidance_count']:>5} {m['late_guidance_count']:>4} {str(m['resolved']):>8}")

    # Aggregates
    n = len(metrics_list)
    if n > 0:
        print("-" * 110)
        avg_hit5 = sum(m["l1_hit_5"] for m in metrics_list) / n
        avg_mrr = sum(m["l1_mrr"] for m in metrics_list) / n
        avg_actions = sum(m["action_count"] for m in metrics_list) / n
        avg_prec = sum(m["edit_file_precision"] for m in metrics_list) / n
        avg_fb4 = sum(m["files_viewed_before_gold"] for m in metrics_list) / n
        total_bridges = sum(m["l3b_bridge_events"] for m in metrics_list)
        total_stale = sum(m["stale_guidance_count"] for m in metrics_list)
        total_late = sum(m["late_guidance_count"] for m in metrics_list)
        resolved_count = sum(m["resolved"] for m in metrics_list)
        print(f"{'AVERAGE':<20} {avg_hit5:>5.2f} {avg_mrr:>5.2f} {'':>8} {avg_fb4:>8.1f} {avg_actions:>7.0f} {avg_prec:>9.2f} {total_bridges:>7} {total_stale:>5} {total_late:>4} {resolved_count:>6}/{n:<2}")


def main():
    parser = argparse.ArgumentParser(description="Localization metrics harness")
    parser.add_argument("--artifact-dirs", nargs="+", help="Directories containing task artifacts")
    parser.add_argument("--label", default="", help="Label for output")
    args = parser.parse_args()

    if not args.artifact_dirs:
        print("Usage: python scripts/localization_metrics.py --artifact-dirs /tmp/task1 /tmp/task2 ...")
        sys.exit(1)

    metrics_list = []
    for d in args.artifact_dirs:
        task_id = os.path.basename(d).replace("task-", "").replace("5task_", "").replace("full_", "")
        for f in glob.glob(f"{d}/results/**/output.jsonl", recursive=True):
            m = compute_task_metrics(f, task_id)
            metrics_list.append(m)
            break

    print_metrics_table(metrics_list, args.label)


if __name__ == "__main__":
    main()
