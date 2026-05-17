from __future__ import annotations
import json
import os
from typing import Any
from scripts.analysis.trajectory_parser import AgentTrajectory, AgentAction
from scripts.analysis.test_command_classifier import classify_test_command

def join_gt_to_agent(gt_events_path: str, trajectory: AgentTrajectory, edited_files: set[str], edited_symbols: set[str], reaction_window: int = 15) -> list[dict[str, Any]]:
    """Join GT layer events to agent reactions by iteration number."""
    if not os.path.exists(gt_events_path):
        return []

    events = []
    with open(gt_events_path, encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue

    actions_by_iter = {a.iter: a for a in trajectory.actions}
    reactions = []

    for evt in events:
        if not evt.get("next_action_type"):
            continue
        # Only measure suggestions that have a concrete file — no file = unmeasurable by design
        if not evt.get("next_action_file"):
            continue

        gt_iter = evt.get("iter", 0)
        window_actions = [actions_by_iter[i] for i in range(gt_iter + 1, gt_iter + 1 + reaction_window) if i in actions_by_iter]

        reaction = {
            "schema_version": "1.0.0",
            "run_id": evt.get("run_id", ""),
            "task_id": evt.get("task_id", ""),
            "gt_event_id": evt.get("event_id", ""),
            "gt_layer": evt.get("layer", ""),
            "gt_iter": gt_iter,
            "gt_next_action_type": evt.get("next_action_type"),
            "gt_next_action_file": evt.get("next_action_file"),
            "gt_next_action_command": evt.get("next_action_command"),
            "gt_next_action_test": evt.get("next_action_test"),
            "reaction_window": reaction_window,
            "checked_until_iter": gt_iter + reaction_window,
        }

        follow = compute_follow_type(evt, window_actions, edited_files, edited_symbols)
        reaction.update(follow)
        reactions.append(reaction)

    return reactions

def _file_match(gt_file: str, candidate: str) -> bool:
    """Check if gt_file matches candidate via substring or basename."""
    if not gt_file or not candidate:
        return False
    if gt_file in candidate or candidate in gt_file:
        return True
    # Basename match (handles path prefix differences)
    import os
    gt_base = os.path.basename(gt_file)
    cand_base = os.path.basename(candidate)
    if gt_base and cand_base and gt_base == cand_base:
        return True
    return False


def compute_follow_type(gt_event: dict, window_actions: list[AgentAction], edited_files: set[str], edited_symbols: set[str]) -> dict[str, Any]:
    """Compute structural follow-through.

    Checks file path match (read/edit), command content match (grep/search),
    and symbol match across the full window. Not just static path substring.
    """
    result: dict[str, Any] = {
        "followed_within_1": False, "followed_within_3": False, "followed_within_5": False,
        "followed_eventually": False, "follow_type": "NOT_MEASURABLE",
        "ignored": False, "partial_follow": False, "contradicted": False,
        "finished_without_follow": False,
        "ran_broad_test_after_gt": False, "ran_targeted_test_after_gt": False,
        "ran_related_test_after_gt": False, "ran_irrelevant_test_after_gt": False,
        "opened_suggested_file": False, "edited_suggested_file": False,
        "changed_diff_after_gt": False,
    }

    if not window_actions:
        result["not_measurable_reason"] = "no_actions_in_window"
        return result

    gt_file = gt_event.get("next_action_file", "")
    gt_type = gt_event.get("next_action_type", "")

    for i, act in enumerate(window_actions):
        if act.action_type == "finish":
            result["finished_without_follow"] = True
            if i == 0:
                result["follow_type"] = "CONTRADICTED"
                result["contradicted"] = True
            break

        matched = False

        # Check 1: file path match (read/edit actions)
        if gt_file and act.file_path and _file_match(gt_file, act.file_path):
            matched = True
            if act.action_type == "read_file":
                result["opened_suggested_file"] = True
            elif act.action_type == "edit_file":
                result["edited_suggested_file"] = True

        # Check 2: command content match (grep/search/run that references the file)
        if not matched and gt_file and act.action_type == "run_command" and act.command:
            import os
            gt_basename = os.path.basename(gt_file)
            gt_stem = os.path.splitext(gt_basename)[0]
            if gt_file in act.command or gt_basename in act.command:
                matched = True
                result["opened_suggested_file"] = True
            elif gt_stem and len(gt_stem) > 3 and gt_stem in act.command:
                matched = True
                result["partial_follow"] = True

        if matched:
            if i == 0: result["followed_within_1"] = True
            if i < 3: result["followed_within_3"] = True
            if i < 5: result["followed_within_5"] = True
            result["followed_eventually"] = True
            if act.action_type == "read_file" or act.action_type == "edit_file":
                result["follow_type"] = "FOLLOWED_EXACT" if _file_match(gt_file, act.file_path or "") else "FOLLOWED_RELATED_FILE"
            else:
                result["follow_type"] = "FOLLOWED_RELATED_FILE"

        # Check test commands
        if act.action_type == "run_command" and act.command:
            kind = classify_test_command(act.command, edited_files, edited_symbols)
            if kind == "broad_project_verification": result["ran_broad_test_after_gt"] = True
            elif kind.startswith("targeted"): result["ran_targeted_test_after_gt"] = True
            elif kind == "irrelevant_verification": result["ran_irrelevant_test_after_gt"] = True

    if not result["followed_eventually"] and not result["finished_without_follow"]:
        result["follow_type"] = "IGNORED"
        result["ignored"] = True

    return result


def _load_jsonl(path: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                records.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    return records


if __name__ == "__main__":
    import argparse
    import glob
    import sys

    parser = argparse.ArgumentParser(description="Post-hoc GT event to agent reaction joiner")
    parser.add_argument("--gt-events", required=True, help="gt_layer_events JSONL glob")
    parser.add_argument("--gt-interactions", default="", help="gt_interactions JSONL glob")
    parser.add_argument("--infer-logs", default="", help="Directory with infer/instance logs")
    parser.add_argument("--output-jsonl", default="", help="output.jsonl glob for final outcome")
    parser.add_argument("--out-reactions", required=True, help="Output reactions JSONL path")
    parser.add_argument("--out-summary", default="", help="Output summary JSON path")
    args = parser.parse_args()

    gt_files = glob.glob(args.gt_events)
    if not gt_files:
        print(f"WARN: No GT event files matching {args.gt_events} (possibly baseline run)", file=sys.stderr)
        with open(args.out_reactions, "w") as f:
            pass
        if args.out_summary:
            with open(args.out_summary, "w") as f:
                json.dump({"total_gt_events": 0, "reactions_produced": 0, "note": "no_gt_events"}, f)
        sys.exit(0)

    all_events: list[dict] = []
    for gf in gt_files:
        all_events.extend(_load_jsonl(gf))

    next_action_events = [e for e in all_events if e.get("next_action_type")]
    print(f"Loaded {len(all_events)} GT events, {len(next_action_events)} with next_action")

    interaction_files = glob.glob(args.gt_interactions) if args.gt_interactions else []
    interactions: list[dict] = []
    for inf in interaction_files:
        interactions.extend(_load_jsonl(inf))

    # Parse REAL agent trajectory from output.jsonl (not mock from interaction logs)
    real_actions: list[AgentAction] = []
    output_files = glob.glob(args.output_jsonl) if args.output_jsonl else []
    for of in output_files:
        try:
            with open(of, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        record = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    history = record.get("history", [])
                    action_idx = 0
                    for entry in history:
                        action = entry.get("action", "")
                        entry_args = entry.get("args", {})
                        if action in ("run", "read", "write", "edit", "browse", "finish"):
                            act = AgentAction(
                                iter=action_idx,
                                action_type={
                                    "run": "run_command",
                                    "read": "read_file",
                                    "write": "edit_file",
                                    "edit": "edit_file",
                                    "browse": "read_file",
                                    "finish": "finish",
                                }.get(action, action),
                                file_path=entry_args.get("path", "") or None,
                                command=entry_args.get("command", "") or None,
                            )
                            real_actions.append(act)
                            action_idx += 1
        except Exception as e:
            print(f"WARN: Failed to parse {of}: {e}", file=sys.stderr)

    if not real_actions:
        # Fallback to interaction log mock (degraded mode)
        print("WARN: No real trajectory from output.jsonl, falling back to interaction log mock", file=sys.stderr)
        for ix, entry in enumerate(interactions):
            trigger = entry.get("trigger", "")
            act = AgentAction(
                iter=entry.get("iter", ix),
                action_type="run_command" if "cmd" in trigger else "edit_file" if "edit" in trigger else "read_file",
                file_path=trigger.split(":")[-1] if ":" in trigger else None,
                command=entry.get("agent_action_after", ""),
            )
            real_actions.append(act)
        print(f"Fallback: {len(real_actions)} mock actions from interaction log", file=sys.stderr)
    else:
        print(f"Parsed {len(real_actions)} real agent actions from output.jsonl")

    traj = AgentTrajectory(
        task_id=all_events[0].get("task_id", "") if all_events else "",
        run_id=all_events[0].get("run_id", "") if all_events else "",
        actions=real_actions,
        total_iterations=len(real_actions),
    )

    reactions: list[dict] = []
    for gf in gt_files:
        r = join_gt_to_agent(gf, traj, set(), set())
        reactions.extend(r)

    with open(args.out_reactions, "w", encoding="utf-8") as f:
        for r in reactions:
            f.write(json.dumps(r) + "\n")

    summary = {
        "total_gt_events": len(all_events),
        "next_action_events": len(next_action_events),
        "reactions_produced": len(reactions),
        "follow_type_distribution": {},
    }
    for r in reactions:
        ft = r.get("follow_type", "?")
        summary["follow_type_distribution"][ft] = summary["follow_type_distribution"].get(ft, 0) + 1

    if args.out_summary:
        with open(args.out_summary, "w") as f:
            json.dump(summary, f, indent=2)

    print(f"Wrote {len(reactions)} reactions to {args.out_reactions}")
    measurable = [e for e in next_action_events if e.get("next_action_file")]
    if measurable and not reactions:
        print("WARN: measurable next_action events exist but zero reactions produced", file=sys.stderr)
