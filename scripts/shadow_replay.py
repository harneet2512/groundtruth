"""Shadow-replay archived ``output.jsonl`` artifacts through FINAL_ARCH_V2.

Replays each agent action (read/edit/finish) through:

- a freshly-constructed Layer 2 ``AgentState``
- the Layer 3 ``CollaborationRouter``
- the Layer 4 providers (called by the router)

For every event the script records:

- old hook would emit?  (proxy: was there a ``[GT]`` marker in the agent's NEXT
  observation message in the archived trace?)
- new router would emit?
- if not: the router's suppression reason + detail
- whether the agent followed the router's proposed next_action_file
- whether the evidence (or follow-up) referenced a gold file BEFORE the agent's
  first read of that gold file
- whether the evidence pointed at a file the agent had already viewed (stale)
- whether the evidence arrived after the agent had already edited the target
  function (late)

The report is descriptive. It does NOT claim GT helps or hurts. Product claims
require paired GT-vs-baseline runs on unseen tasks (FINAL_ARCH_V2 §6).

Per-task graph.db resolution:
- ``--graph-dir DIR``           : looks up ``DIR/<task_id>/graph.db``
- ``--graph-map FILE``          : JSON ``{task_id: path}`` lookup
- ``--db PATH``                 : single fallback graph.db used when neither
                                  matches (mostly useful for stress tests)
- If nothing matches, the router classifies emissions as ``NO_GRAPH_DB`` —
  which is distinct from ``NO_EVIDENCE`` ("graph present, empty for this file").

Usage:
    python scripts/shadow_replay.py \\
        --outputs '.tmp_diag_artifacts/task-*/results/**/output.jsonl' \\
        --graph-dir .tmp_holdout/bugs \\
        --report   reports/shadow_replay/v2_layer3_replay.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Allow running directly from the repo without "pip install -e .".
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from groundtruth.router import CollaborationRouter, RouterEmission
from groundtruth.router.decisions import SuppressionReason
from groundtruth.state.agent_state import AgentState, canonical_repo_path


_GT_MARKER = re.compile(r"\[GT[_\] ]|<gt-")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_gold_files(test_result: dict[str, Any]) -> set[str]:
    gold: set[str] = set()
    patch = test_result.get("git_patch", "") if isinstance(test_result, dict) else ""
    if not isinstance(patch, str):
        return gold
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            gold.add(line[len("+++ b/"):].strip())
    return gold


def _extract_function_names_from_edit(args: dict[str, Any]) -> list[str]:
    fns: list[str] = []
    for key in ("old_str", "new_str", "file_text"):
        text = args.get(key, "") if isinstance(args, dict) else ""
        if not isinstance(text, str) or not text:
            continue
        for m in re.finditer(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.MULTILINE):
            if m.group(1) not in fns:
                fns.append(m.group(1))
            if len(fns) >= 3:
                return fns
        for m in re.finditer(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.MULTILINE):
            if m.group(1) not in fns:
                fns.append(m.group(1))
            if len(fns) >= 3:
                return fns
    return fns


def _next_obs_text(history: list[dict[str, Any]], start: int, depth: int = 1) -> str:
    parts: list[str] = []
    seen = 0
    for ev in history[start + 1:]:
        if ev.get("source") == "agent" and ev.get("action"):
            break
        msg = ev.get("message") or ""
        if isinstance(msg, str):
            parts.append(msg)
        seen += 1
        if seen >= depth:
            break
    return "\n".join(parts)


def _action_count_up_to(history: list[dict[str, Any]], idx: int) -> int:
    """Match localization_metrics: count actions other than think/recall/message."""
    n = 0
    for ev in history[: idx + 1]:
        a = ev.get("action")
        if a and a not in ("think", "recall", "message"):
            n += 1
    return n


def _is_gold_path(path: str, gold_files: set[str], repo_root: str) -> bool:
    canon = canonical_repo_path(path, repo_root)
    if not canon:
        return False
    for g in gold_files:
        if g == canon or g.endswith("/" + canon) or canon.endswith("/" + g):
            return True
        if os.path.basename(g) == os.path.basename(canon):
            return True
    return False


# ---------------------------------------------------------------------------
# Per-task graph.db resolution
# ---------------------------------------------------------------------------


def _resolve_graph_db(
    instance_id: str,
    *,
    graph_dir: str = "",
    graph_map: dict[str, str] | None = None,
    fallback_db: str = "",
) -> str:
    """Return the best graph.db path for ``instance_id`` or ``""``.

    Resolution order:
    1. Explicit map (``graph_map[task]`` or ``graph_map[task]`` shortened).
    2. ``<graph_dir>/<task_id>/graph.db``
    3. ``<graph_dir>/<basename-of-task_id-after-double-underscore>/graph.db``
       (matches ``aws-cloudformation__cfn-lint-3821`` → ``cfn-lint-3821``)
    4. The single ``--db`` fallback.
    Returns the first path that exists.
    """
    candidates: list[str] = []
    if graph_map:
        if instance_id in graph_map:
            candidates.append(graph_map[instance_id])
        short = instance_id.split("__", 1)[-1]
        if short and short != instance_id and short in graph_map:
            candidates.append(graph_map[short])
    if graph_dir:
        candidates.append(os.path.join(graph_dir, instance_id, "graph.db"))
        short = instance_id.split("__", 1)[-1]
        if short and short != instance_id:
            candidates.append(os.path.join(graph_dir, short, "graph.db"))
    if fallback_db:
        candidates.append(fallback_db)
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


# ---------------------------------------------------------------------------
# Replay one task
# ---------------------------------------------------------------------------


def _replay_one(
    output_path: str,
    *,
    db_path: str,
    repo_root: str,
) -> dict[str, Any]:
    with open(output_path, encoding="utf-8") as fh:
        line = fh.readline()
    if not line:
        return {"output_path": output_path, "error": "empty"}
    data = json.loads(line)
    history: list[dict[str, Any]] = data.get("history") or []
    instance_id = data.get("instance_id") or data.get("instance", {}).get("instance_id") or "?"
    test_result = data.get("test_result") or {}
    gold_files = _extract_gold_files(test_result)

    state = AgentState.create(
        task_id=instance_id,
        max_iterations=int(data.get("metadata", {}).get("max_iterations") or 100),
        repo_root=repo_root,
    )
    instruction = data.get("instruction") or ""
    if isinstance(instruction, str) and "<gt-task-brief>" in instruction:
        cands: list[str] = []
        for m in re.finditer(r"^\d+\.\s+([^\s—:]+)", instruction, re.MULTILINE):
            cands.append(m.group(1))
        if cands:
            state.set_brief_candidates(cands)

    router = CollaborationRouter(state, db_path, repo_root)

    summary: dict[str, Any] = {
        "output_path": output_path,
        "instance_id": instance_id,
        "task_id_for_state": state.task_id,
        "history_len": len(history),
        "gold_files": sorted(gold_files),
        "graph_db": db_path,
        "graph_db_present": bool(db_path) and os.path.isfile(db_path),
        "events": [],
        "router_emit_count": 0,
        "router_suppression_count": defaultdict(int),
        "old_hook_emit_count": 0,
        "bridge_event_before_gold": 0,
        "stale_guidance_count": 0,
        "late_guidance_count": 0,
        "injections_per_task": 0,
        "provider_request_count": 0,
        "provider_empty_count": 0,
        "provider_request_log": [],
        "agent_followed_gt_edge": 0,
        "first_gold_view_idx": None,
        "first_gold_view_action_count": None,
        "distinct_files_viewed_before_gold": 0,
        "action_count_total": 0,
    }
    first_gold_view_idx: int | None = None
    distinct_files_before_gold: set[str] = set()
    edited_files_canonical: set[str] = set()

    for idx, ev in enumerate(history):
        action = ev.get("action")
        args = ev.get("args") or {}
        path = args.get("path") if isinstance(args, dict) else ""
        action_count = _action_count_up_to(history, idx)
        state.set_iteration(action_count)
        is_edit = action == "edit" or "str_replace" in str(
            args.get("command", "") if isinstance(args, dict) else ""
        )
        is_read = action == "read"
        if is_read and path:
            canon = state.record_view(path, sync_legacy_file=False)
            if canon:
                if first_gold_view_idx is None:
                    if _is_gold_path(canon, gold_files, repo_root):
                        first_gold_view_idx = idx
                        summary["first_gold_view_idx"] = idx
                        summary["first_gold_view_action_count"] = action_count
                    else:
                        # Distinct files viewed *before* the gold file. Repaired
                        # metric: counts unique files, not the action index.
                        distinct_files_before_gold.add(canon)
        # For edits we capture whether this file was *previously* edited
        # before we update edited_files_canonical — so "late" really means
        # "edit-after-edit on the same target", not "we recorded the file
        # just now in this same step".
        edited_before_this_step: bool = (
            is_edit
            and bool(path)
            and canonical_repo_path(path, repo_root) in edited_files_canonical
        )
        # Old-hook proxy.
        next_text = _next_obs_text(history, idx, depth=1)
        old_emit = bool(_GT_MARKER.search(next_text))
        if old_emit:
            summary["old_hook_emit_count"] += 1
        # Run router.
        em: RouterEmission | None = None
        if is_read and path:
            em = router.on_view(path)
        elif is_edit and path:
            fns = _extract_function_names_from_edit(args)
            em = router.on_edit(path, fns)
        # Now record the edit in AgentState + the per-task set, AFTER the
        # router has seen the "was this previously edited?" view above.
        if is_edit and path:
            canon = state.record_edit(path)
            if canon:
                edited_files_canonical.add(canon)
        if em is not None:
            event_record: dict[str, Any] = {
                "idx": idx,
                "action_count": action_count,
                "action": action,
                "path": path,
                "old_emit": old_emit,
                "router_emit": em.emit,
                "router_kind": em.kind.value,
                "router_reason": em.suppression_reason.value if em.suppression_reason else None,
                "router_detail": em.suppression_detail,
                "primary_edge_file": em.primary_edge_file,
                "next_action_type": em.next_action_type,
                "next_action_file": em.next_action_file,
                "old_vs_new": (
                    "both_emit" if old_emit and em.emit
                    else "old_only" if old_emit
                    else "new_only" if em.emit
                    else "both_silent"
                ),
            }
            summary["events"].append(event_record)
            if em.emit:
                summary["router_emit_count"] += 1
                summary["injections_per_task"] += 1
                if (
                    em.primary_edge_file
                    and gold_files
                    and _is_gold_path(em.primary_edge_file, gold_files, repo_root)
                    and (first_gold_view_idx is None or idx < first_gold_view_idx)
                ):
                    summary["bridge_event_before_gold"] += 1
                primary_canon = canonical_repo_path(em.primary_edge_file, repo_root)
                if primary_canon and primary_canon in state.visited_files_set():
                    summary["stale_guidance_count"] += 1
                if is_edit and edited_before_this_step:
                    # "Late" here = router fired on an edit whose file the
                    # agent had ALREADY edited previously in the trajectory
                    # (re-edit). Distinct from stale-edge classification.
                    summary["late_guidance_count"] += 1
                # Agent-followed-gt-edge.
                for follow in history[idx + 1: idx + 6]:
                    if follow.get("action") != "read":
                        continue
                    f_args = follow.get("args") or {}
                    f_path = f_args.get("path") if isinstance(f_args, dict) else ""
                    if f_path and primary_canon:
                        f_canon = canonical_repo_path(f_path, repo_root)
                        if (
                            f_canon == primary_canon
                            or f_canon.endswith("/" + primary_canon)
                            or primary_canon.endswith("/" + f_canon)
                        ):
                            summary["agent_followed_gt_edge"] += 1
                            break
            else:
                reason = em.suppression_reason.value if em.suppression_reason else "unknown"
                summary["router_suppression_count"][reason] += 1
        summary["action_count_total"] = max(summary["action_count_total"], action_count)
    summary["router_suppression_count"] = dict(summary["router_suppression_count"])
    # Drain provider counters from the router.
    summary["provider_request_count"] = router.provider_request_count
    summary["provider_empty_count"] = router.provider_empty_count
    summary["provider_request_log"] = router.provider_request_log
    summary["distinct_files_viewed_before_gold"] = len(distinct_files_before_gold)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--outputs",
        nargs="+",
        required=True,
        help="One or more output.jsonl paths (globs allowed).",
    )
    parser.add_argument(
        "--graph-dir",
        default="",
        help="Directory containing <task_id>/graph.db sub-folders.",
    )
    parser.add_argument(
        "--graph-map",
        default="",
        help="JSON file mapping task_id -> graph.db path.",
    )
    parser.add_argument(
        "--db",
        default="",
        help="Fallback single graph.db when no per-task match exists.",
    )
    parser.add_argument(
        "--repo-root",
        default="/workspace",
        help="Prefix to strip when canonicalizing paths.",
    )
    parser.add_argument(
        "--report",
        default="reports/shadow_replay/replay.json",
        help="Output JSON path for the aggregated report.",
    )
    args = parser.parse_args()

    paths: list[str] = []
    for spec in args.outputs:
        if any(ch in spec for ch in "*?["):
            paths.extend(sorted(glob.glob(spec, recursive=True)))
        else:
            paths.append(spec)
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        print("No output.jsonl files found.", file=sys.stderr)
        return 1

    graph_map: dict[str, str] = {}
    if args.graph_map:
        try:
            graph_map = json.loads(Path(args.graph_map).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"--graph-map: {exc}", file=sys.stderr)
            return 2

    per_task: list[dict[str, Any]] = []
    matched_graph = 0
    for p in paths:
        # Peek the task id to resolve a graph.db.
        try:
            with open(p, encoding="utf-8") as fh:
                first = fh.readline()
            top = json.loads(first)
            instance_id = top.get("instance_id") or top.get("instance", {}).get("instance_id") or ""
        except Exception:
            instance_id = ""
        resolved_db = _resolve_graph_db(
            instance_id,
            graph_dir=args.graph_dir,
            graph_map=graph_map,
            fallback_db=args.db,
        )
        if resolved_db:
            matched_graph += 1
        per_task.append(
            _replay_one(p, db_path=resolved_db, repo_root=args.repo_root)
        )

    # Aggregate.
    agg: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "input_count": len(paths),
        "graph_resolved_count": matched_graph,
        "graph_unresolved_count": len(paths) - matched_graph,
        "graph_dir": args.graph_dir,
        "graph_map_file": args.graph_map,
        "fallback_db": args.db,
        "repo_root": args.repo_root,
        "totals": {
            "router_emit": sum(t.get("router_emit_count", 0) for t in per_task),
            "old_hook_emit": sum(t.get("old_hook_emit_count", 0) for t in per_task),
            "provider_request": sum(t.get("provider_request_count", 0) for t in per_task),
            "provider_empty": sum(t.get("provider_empty_count", 0) for t in per_task),
            "bridge_event_before_gold": sum(t.get("bridge_event_before_gold", 0) for t in per_task),
            "stale_guidance_count": sum(t.get("stale_guidance_count", 0) for t in per_task),
            "late_guidance_count": sum(t.get("late_guidance_count", 0) for t in per_task),
            "injections_per_task_total": sum(t.get("injections_per_task", 0) for t in per_task),
            "agent_followed_gt_edge": sum(t.get("agent_followed_gt_edge", 0) for t in per_task),
        },
        "old_vs_new_distribution": dict(
            Counter(
                evrec["old_vs_new"]
                for t in per_task
                for evrec in t.get("events", [])
            )
        ),
        "suppression_distribution": dict(
            Counter(
                {
                    reason: sum(
                        t.get("router_suppression_count", {}).get(reason, 0) for t in per_task
                    )
                    for reason in {r.value for r in SuppressionReason}
                }
            )
        ),
        "tasks": per_task,
        "notes": [
            "Internal/offline tests are admission gates only.",
            "This report describes what happened, it does not claim GT helps or hurts.",
            "Product claims require paired GT-vs-baseline runs on unseen tasks.",
            "NO_GRAPH_DB is distinct from NO_EVIDENCE: the former means the graph file was absent for the task; the latter means the graph was present and returned no rows for this file.",
        ],
    }

    out_path = Path(args.report)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(agg, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Shadow replay report written to {out_path}")
    print(
        f"  inputs={agg['input_count']} graph_resolved={agg['graph_resolved_count']} "
        f"graph_unresolved={agg['graph_unresolved_count']}"
    )
    print(f"  totals={agg['totals']}")
    print(f"  suppression={agg['suppression_distribution']}")
    print(f"  old_vs_new={agg['old_vs_new_distribution']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
