from __future__ import annotations
import json
import os
from typing import Any
from scripts.analysis.trajectory_parser import AgentTrajectory, AgentAction
from scripts.analysis.test_command_classifier import classify_test_command

def join_gt_to_agent(gt_events_path: str, trajectory: AgentTrajectory, edited_files: set[str], edited_symbols: set[str], reaction_window: int = 5) -> list[dict[str, Any]]:
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

def compute_follow_type(gt_event: dict, window_actions: list[AgentAction], edited_files: set[str], edited_symbols: set[str]) -> dict[str, Any]:
    """Compute structural follow-through."""
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

        # Check file match
        if gt_file and act.file_path:
            if gt_file in act.file_path or act.file_path in gt_file:
                if act.action_type == "read_file":
                    result["opened_suggested_file"] = True
                elif act.action_type == "edit_file":
                    result["edited_suggested_file"] = True

                if i == 0: result["followed_within_1"] = True
                if i < 3: result["followed_within_3"] = True
                if i < 5: result["followed_within_5"] = True
                result["followed_eventually"] = True
                result["follow_type"] = "FOLLOWED_EXACT" if act.action_type == gt_type.replace("run_targeted_test", "run_command") else "FOLLOWED_RELATED_FILE"

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
