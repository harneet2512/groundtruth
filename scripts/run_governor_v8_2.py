"""Run the v8.2 trace-gated scheduler experiment."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from groundtruth.pretask.v7_4_brief import run_v74
from groundtruth.pretask.v8_governor import govern, normalize_path
from groundtruth.pretask.v8_2_scheduler import (
    agent_only_files,
    dumb_bounded_union_files,
    mode_hit,
    parse_trace_artifact,
    schedule_v82,
    trace_events_to_agent_candidates,
)


EVAL_START = 20
EVAL_END = 60
PILOT_N = 10


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def load_weights(path: Path) -> dict[str, float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: float(v) for k, v in raw.items() if k.startswith("W_")}


def eval_bugs(holdout: Path) -> list[dict[str, Any]]:
    return load_jsonl(holdout)[EVAL_START:EVAL_END]


def select_pilot(bugs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bug in sorted(bugs, key=lambda b: (str(b["repo"]), str(b["bug_id"]))):
        by_repo[str(bug["repo"])].append(bug)
    repos = sorted(by_repo)
    selected: list[dict[str, Any]] = []
    i = 0
    while len(selected) < PILOT_N:
        progressed = False
        for repo in repos:
            if i < len(by_repo[repo]):
                selected.append(by_repo[repo][i])
                progressed = True
                if len(selected) >= PILOT_N:
                    break
        if not progressed:
            break
        i += 1
    return selected


def v75_ranked(bug: dict[str, Any], weights: dict[str, float]) -> dict[str, Any]:
    return run_v74(
        issue_text=bug.get("issue_body") or bug.get("issue_title") or "",
        repo_root=bug["repo_path"],
        graph_db=bug["graph_db_path"],
        bug_id=bug["bug_id"],
        repo=bug["repo"],
        gold_files=bug["gold_files"],
        ablation="C",
        k_anchor=3,
        k_sem_top=10,
        tau_anchor=0.20,
        max_depth=3,
        min_confidence=0.5,
        weights=weights,
        focus_size=3,
    ).__dict__


def row_for_task(
    bug: dict[str, Any],
    weights: dict[str, float],
    artifact_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ranked_result = v75_ranked(bug, weights)
    ranked_full = ranked_result["ranked_full"]
    trace = parse_trace_artifact(artifact_root, str(bug["bug_id"]))
    agent_files = trace.agent_files
    agent_candidates = trace_events_to_agent_candidates(agent_files)
    static = govern(
        ranked_full,
        agent_candidates,
        graph_db=bug["graph_db_path"],
        early_trace_text=trace.early_trace_text,
        preferred_max=5,
        hard_ceiling=7,
    )
    v82 = schedule_v82(ranked_full, bug["graph_db_path"], trace.events)

    gold = bug.get("gold_files") or []
    agent_paths = agent_only_files(agent_files)
    dumb_paths = dumb_bounded_union_files(ranked_full, agent_files)
    static_paths = [c.path for c in static.active_set]
    top3 = [normalize_path(str(rec.get("path", ""))) for rec in ranked_full[:3]]
    unsupported_static_dropped = [
        path for path in static_paths if path not in v82.active_files and path not in agent_files
    ]
    hits = {
        "agent_only": mode_hit(agent_paths, gold),
        "dumb_bounded_union": mode_hit(dumb_paths, gold),
        "static_v8": mode_hit(static_paths, gold),
        "v8_2_scheduler": mode_hit(v82.active_files, gold),
    }
    if hits["v8_2_scheduler"] and not hits["dumb_bounded_union"]:
        gov_vs_dumb = "beat"
    elif hits["dumb_bounded_union"] and not hits["v8_2_scheduler"]:
        gov_vs_dumb = "lost"
    else:
        gov_vs_dumb = "tied"

    event_row = {
        "bug_id": bug["bug_id"],
        "repo": bug["repo"],
        "trace_status": trace.status,
        "artifact": trace.artifact,
        "action_steps": trace.action_steps,
        "events": [
            {"path": e.path, "signal": e.signal, "tier": e.tier, "step": e.step}
            for e in trace.events
        ],
        "agent_files": {
            path: {
                "tier": ev.tier,
                "event_count": ev.event_count,
                "first_step": ev.first_step,
                "signals": list(ev.signals),
            }
            for path, ev in sorted(agent_files.items())
        },
        "error": trace.error,
    }
    comparison_row = {
        "bug_id": bug["bug_id"],
        "repo": bug["repo"],
        "gold_files": gold,
        "trace_status": trace.status,
        "agent_artifact": trace.artifact,
        "gt_top3": top3,
        "agent_only_files": agent_paths,
        "agent_only_gold_inclusion": hits["agent_only"],
        "agent_only_file_count": len(agent_paths),
        "dumb_bounded_union_files": dumb_paths,
        "dumb_bounded_union_gold_inclusion": hits["dumb_bounded_union"],
        "dumb_bounded_union_file_count": len(dumb_paths),
        "static_v8_files": static_paths,
        "static_v8_gold_inclusion": hits["static_v8"],
        "static_v8_file_count": len(static_paths),
        "v8_2_scheduler_files": v82.active_files,
        "v8_2_scheduler_gold_inclusion": hits["v8_2_scheduler"],
        "v8_2_scheduler_file_count": len(v82.active_files),
        "v8_2_active_set": [candidate.__dict__ for candidate in v82.active_set],
        "v8_2_dropped_files": v82.dropped_files,
        "v8_2_structural_added": v82.structural_added,
        "v8_2_provisional_gt_anchors": v82.provisional_gt_anchors,
        "governor_vs_dumb_union": gov_vs_dumb,
        "static_v8_vs_v8_2_file_count_delta": len(static_paths) - len(v82.active_files),
        "v8_2_dropped_unsupported_static_v8_files": unsupported_static_dropped,
        "gt_improved_agent_only_gold_inclusion": hits["v8_2_scheduler"] and not hits["agent_only"],
        "gt_reduced_bloat_vs_dumb_union": hits["v8_2_scheduler"] == hits["dumb_bounded_union"]
        and len(v82.active_files) < len(dumb_paths),
        "gt_reduced_bloat_vs_static_v8": hits["v8_2_scheduler"] == hits["static_v8"]
        and len(v82.active_files) < len(static_paths),
        "v75_first_gold_rank_full": ranked_result["first_gold_rank_full"],
        "v75_candidate_set_size": ranked_result["candidate_set_size"],
    }
    return event_row, comparison_row


def mean_count(rows: list[dict[str, Any]], key: str) -> float:
    return round(statistics.mean(int(row[key]) for row in rows), 4) if rows else 0.0


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    modes = ("agent_only", "dumb_bounded_union", "static_v8", "v8_2_scheduler")
    invalid = [row["bug_id"] for row in rows if row["trace_status"] != "ok"]
    hard_hits = {
        mode: sum(row[f"{mode}_file_count"] >= 7 for row in rows) / max(len(rows), 1)
        for mode in modes
    }
    v82_mean = mean_count(rows, "v8_2_scheduler_file_count")
    dumb_mean = mean_count(rows, "dumb_bounded_union_file_count")
    agent_mean = mean_count(rows, "agent_only_file_count")
    v82_gold = sum(row["v8_2_scheduler_gold_inclusion"] for row in rows)
    dumb_gold = sum(row["dumb_bounded_union_gold_inclusion"] for row in rows)
    agent_gold = sum(row["agent_only_gold_inclusion"] for row in rows)
    worse_than_dumb = sum(
        row["dumb_bounded_union_gold_inclusion"] and not row["v8_2_scheduler_gold_inclusion"]
        for row in rows
    )
    better_than_agent = sum(
        row["v8_2_scheduler_gold_inclusion"] and not row["agent_only_gold_inclusion"]
        for row in rows
    )
    equal_dumb_savings = [
        row["dumb_bounded_union_file_count"] - row["v8_2_scheduler_file_count"]
        for row in rows
        if row["dumb_bounded_union_gold_inclusion"] == row["v8_2_scheduler_gold_inclusion"]
    ]
    stop = {
        "pilot_valid": len(invalid) == 0 and len(rows) == PILOT_N,
        "worse_than_dumb_count": worse_than_dumb,
        "v8_2_better_than_agent_count": better_than_agent,
        "v8_2_hard_ceiling_hit_rate": round(hard_hits["v8_2_scheduler"], 4),
        "v8_2_mean_active_count": v82_mean,
    }
    stop["PASS"] = (
        stop["pilot_valid"]
        and worse_than_dumb <= 1
        and (v82_gold > agent_gold or (v82_gold == agent_gold and agent_mean - v82_mean >= 1.0))
        and (v82_gold != dumb_gold or dumb_mean - v82_mean >= 0.5)
        and hard_hits["v8_2_scheduler"] <= 0.30
        and v82_mean <= 5.5
    )
    killed = (
        bool(invalid)
        or worse_than_dumb > 1
        or (v82_gold == dumb_gold and v82_mean >= dumb_mean)
        or sum(row["v8_2_scheduler_file_count"] >= 7 for row in rows) > 3
    )
    return {
        "n": len(rows),
        "invalid_trace_count": len(invalid),
        "invalid_trace_tasks": invalid,
        "gold_inclusion_count_by_mode": {
            mode: sum(row[f"{mode}_gold_inclusion"] for row in rows) for mode in modes
        },
        "mean_active_file_count_by_mode": {
            mode: mean_count(rows, f"{mode}_file_count") for mode in modes
        },
        "hard_ceiling_hit_rate_by_mode": {mode: round(rate, 4) for mode, rate in hard_hits.items()},
        "v8_2_worse_than_dumb_count": worse_than_dumb,
        "v8_2_better_than_agent_count": better_than_agent,
        "v8_2_equal_inclusion_file_savings_vs_dumb_union": round(
            statistics.mean(equal_dumb_savings), 4
        )
        if equal_dumb_savings
        else 0.0,
        "v8_2_dropped_unsupported_static_v8_tasks": sum(
            bool(row["v8_2_dropped_unsupported_static_v8_files"]) for row in rows
        ),
        "gt_reduced_bloat_vs_dumb_union_tasks": sum(row["gt_reduced_bloat_vs_dumb_union"] for row in rows),
        "gt_reduced_bloat_vs_static_v8_tasks": sum(row["gt_reduced_bloat_vs_static_v8"] for row in rows),
        "stop_conditions": stop,
        "kill_criteria_hit": killed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--holdout", default="holdout_v1.jsonl")
    parser.add_argument("--weights", default="results/coefficients_v5.json")
    parser.add_argument("--agent-artifact-root", default="results/governor_v8_2_agent_artifacts")
    parser.add_argument("--out-dir", default="results")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    bugs = eval_bugs(Path(args.holdout))
    pilot = select_pilot(bugs)
    write_jsonl(out_dir / "governor_v8_2_pilot_selection.jsonl", pilot)
    spec_audit = {
        "hypothesis": "GT generalizes better as a runtime search-space controller than as a static expander",
        "holdout": args.holdout,
        "eval_slice_zero_based": [EVAL_START, EVAL_END],
        "pilot_n": PILOT_N,
        "selection_rule": "v8.1 deterministic round-robin by sorted repo",
        "agent_artifact_root": args.agent_artifact_root,
        "v75_weights": args.weights,
        "frozen_static_v8_module": "src/groundtruth/pretask/v8_governor.py",
        "scheduler_module": "src/groundtruth/pretask/v8_2_scheduler.py",
        "preferred_max": 5,
        "hard_ceiling": 7,
        "structural_add_cap": 2,
    }
    (out_dir / "governor_v8_2_spec_audit.json").write_text(
        json.dumps(spec_audit, indent=2),
        encoding="utf-8",
    )

    weights = load_weights(Path(args.weights))
    event_rows: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
    for bug in pilot:
        event_row, comparison_row = row_for_task(bug, weights, Path(args.agent_artifact_root))
        event_rows.append(event_row)
        comparison_rows.append(comparison_row)
    write_jsonl(out_dir / "governor_v8_2_trace_events.jsonl", event_rows)
    write_jsonl(out_dir / "governor_v8_2_comparison.jsonl", comparison_rows)

    summary = aggregate(comparison_rows)
    verdict_text = (
        "CONTINUE v8.2"
        if summary["stop_conditions"]["PASS"] and not summary["kill_criteria_hit"]
        else "REVISE ARCHITECTURE BEFORE CONTINUING"
    )
    verdict = {
        "gt_only_verdict": "unchanged_from_static_v8_rejected_baseline",
        "gt_plus_agent_verdict": verdict_text,
        "summary": summary,
    }
    (out_dir / "governor_v8_2_verdict.json").write_text(
        json.dumps(verdict, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
