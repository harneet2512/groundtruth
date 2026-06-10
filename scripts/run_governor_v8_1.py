"""Run the executable v8.1 bounded-governor evaluation.

This is harness code only.  It does not change governor scoring, expansion
policy, v7.5 scoring, or any frozen model behavior.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from groundtruth.pretask.v7_4_brief import run_v74
from groundtruth.pretask.v8_governor import (  # noqa: E402
    AgentCandidate,
    agent_path_allowed,
    dumb_bounded_union,
    govern,
    includes_gold,
    normalize_path,
    pilot_stop_condition,
)


EVAL_START = 20
EVAL_END = 60
PILOT_N = 10
EARLY_ACTION_STEPS = 12

EDIT_MARKERS = ("edit", "write", "create", "insert", "str_replace", "apply_patch", "patch")
OPEN_MARKERS = ("cat", "sed -n", "head", "tail", "less", "view", "open", "read")
SEARCH_MARKERS = ("grep", "rg", "ripgrep", "find", "search", "git grep")
TEST_MARKERS = ("pytest", "unittest", "FAIL", "FAILED", "Traceback", "AssertionError", "Error:")
EXCLUDED_PATH_PARTS = (
    "node_modules/",
    ".venv/",
    "site-packages/",
    "dist-packages/",
    "/usr/",
    "/opt/",
    "/lib/",
)
DIFF_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$", re.MULTILINE)
PATH_RE = re.compile(r"(?P<path>(?:[A-Za-z]:)?(?:/?[\w.-]+/)+[\w.@+-]+\.[A-Za-z0-9_+-]+)")
PY_FRAME_RE = re.compile(r'File "([^"]+)", line \d+')
V8_FRAME_RE = re.compile(r"\bat\s+[^(]+\(([^():]+(?:/[^():]+)+):\d+:\d+\)")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


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


def path_allowed(path: str, evidence: str) -> bool:
    p = normalize_path(path)
    if any(part in f"/{p}" for part in EXCLUDED_PATH_PARTS):
        return False
    return agent_path_allowed(p, evidence)


def event_text(step: Any) -> str:
    if not isinstance(step, dict):
        return str(step)
    chunks: list[str] = []
    for key in ("action", "observation", "response", "content", "message", "tool", "args"):
        value = step.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            chunks.append(json.dumps(value, sort_keys=True))
        else:
            chunks.append(str(value))
    return "\n".join(chunks)


def actionish(step: Any) -> bool:
    return isinstance(step, dict) and any(step.get(k) for k in ("action", "tool", "args"))


def classify_text(text: str) -> list[str]:
    low = text.lower()
    classes: list[str] = []
    if any(marker in low for marker in EDIT_MARKERS):
        classes.append("material_edit")
    if DIFF_RE.search(text):
        classes.append("diff_header")
    if any(marker in low for marker in OPEN_MARKERS):
        classes.append("generic_open")
    if any(marker in low for marker in SEARCH_MARKERS):
        classes.append("generic_search")
    if any(marker in text for marker in TEST_MARKERS):
        classes.append("failing_test_output")
    if PY_FRAME_RE.search(text) or V8_FRAME_RE.search(text):
        classes.append("stack_trace")
    if not classes and PATH_RE.search(text):
        classes.append("explicit_nomination")
    if PATH_RE.search(text) and "explicit_nomination" not in classes:
        classes.append("tool_trace")
    return classes


def extract_paths(text: str, evidence: str) -> list[str]:
    paths: list[str] = []
    for match in DIFF_RE.finditer(text):
        path = normalize_path(match.group("b"))
        if path_allowed(path, evidence) and path not in paths:
            paths.append(path)
    for regex in (PY_FRAME_RE, V8_FRAME_RE):
        for match in regex.finditer(text):
            path = normalize_path(match.group(1))
            if path_allowed(path, evidence) and path not in paths:
                paths.append(path)
    for match in PATH_RE.finditer(text):
        path = normalize_path(match.group("path"))
        if path_allowed(path, evidence) and path not in paths:
            paths.append(path)
    return paths


def evidence_score(evidence_types: set[str], count: int) -> float:
    if evidence_types & {"material_edit", "diff_header"}:
        return 1.0
    if evidence_types & {"stack_trace", "failing_test_output", "tool_trace"}:
        return 0.9
    if evidence_types & {"generic_open", "generic_search"}:
        return 0.7 if count >= 2 else 0.4
    if "explicit_nomination" in evidence_types:
        return 0.5
    return 0.0


def find_agent_artifact(root: Path, bug_id: str) -> Path | None:
    for rel in (
        Path(bug_id) / "trajectory.traj",
        Path(bug_id) / "trajectory.json",
        Path(f"{bug_id}.traj"),
        Path(f"{bug_id}.json"),
    ):
        p = root / rel
        if p.exists():
            return p
    return None


def parse_agent_evidence(root: Path, bug_id: str) -> dict[str, Any]:
    artifact = find_agent_artifact(root, bug_id)
    if artifact is None:
        return {
            "evidence_status": "missing",
            "artifact": None,
            "early_trace_text": "",
            "agent_candidates": [],
            "events": [],
        }
    try:
        raw = json.loads(artifact.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return {
            "evidence_status": "unreadable",
            "artifact": str(artifact),
            "error": str(exc),
            "early_trace_text": "",
            "agent_candidates": [],
            "events": [],
        }
    steps = raw.get("trajectory") if isinstance(raw, dict) else raw
    if isinstance(raw, dict) and steps is None:
        steps = raw.get("history")
    if not isinstance(steps, list):
        return {
            "evidence_status": "invalid_schema",
            "artifact": str(artifact),
            "early_trace_text": "",
            "agent_candidates": [],
            "events": [],
        }

    path_events: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    trace_chunks: list[str] = []
    action_steps = 0
    for idx, step in enumerate(steps):
        if not actionish(step):
            continue
        action_steps += 1
        text = event_text(step)
        trace_chunks.append(text)
        classes = classify_text(text)
        for evidence in classes:
            for path in extract_paths(text, evidence):
                item = path_events.setdefault(path, {"path": path, "evidence_types": set(), "count": 0})
                item["evidence_types"].add(evidence)
                item["count"] += 1
                events.append({"step": idx, "path": path, "evidence": evidence})
        if "material_edit" in classes:
            break
        if action_steps >= EARLY_ACTION_STEPS:
            break

    candidates = []
    for item in path_events.values():
        types = set(item["evidence_types"])
        score = evidence_score(types, int(item["count"]))
        primary = (
            "material_edit"
            if "material_edit" in types
            else "diff_header"
            if "diff_header" in types
            else "stack_trace"
            if "stack_trace" in types
            else "failing_test_output"
            if "failing_test_output" in types
            else "tool_trace"
            if "tool_trace" in types
            else "generic_open"
            if "generic_open" in types
            else "generic_search"
            if "generic_search" in types
            else "explicit_nomination"
        )
        candidates.append(
            {
                "path": item["path"],
                "score": score,
                "evidence": primary,
                "evidence_count": item["count"],
                "evidence_types": sorted(types),
            }
        )
    candidates.sort(key=lambda c: (c["score"], c["evidence_count"], c["path"]), reverse=True)
    return {
        "evidence_status": "ok",
        "artifact": str(artifact),
        "early_trace_text": "\n".join(trace_chunks),
        "agent_candidates": candidates,
        "events": events,
    }


def to_agent_candidates(rows: list[dict[str, Any]]) -> list[AgentCandidate]:
    return [
        AgentCandidate(path=r["path"], score=float(r["score"]), evidence=r["evidence"])
        for r in rows
    ]


def run_gtalone(bugs: list[dict[str, Any]], weights: dict[str, float]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bug in bugs:
        ranked_result = v75_ranked(bug, weights)
        ranked_full = ranked_result["ranked_full"]
        gov = govern(
            ranked_full,
            [],
            graph_db=bug["graph_db_path"],
            early_trace_text=bug.get("issue_body") or bug.get("issue_title") or "",
            preferred_max=5,
            hard_ceiling=7,
        )
        top3 = [r["path"] for r in ranked_full[:3]]
        active = [c.path for c in gov.active_set]
        gold = bug.get("gold_files") or []
        top3_hit = includes_gold(top3, gold)
        bounded_hit = includes_gold(active, gold)
        expansion_hit = includes_gold(gov.expansion_added, gold)
        rows.append(
            {
                "bug_id": bug["bug_id"],
                "repo": bug["repo"],
                "gold_files": gold,
                "gt_top3": top3,
                "gt_top3_gold_inclusion": top3_hit,
                "bounded_region": active,
                "bounded_region_gold_inclusion": bounded_hit,
                "bounded_region_size": len(active),
                "expansion_fired": bool(gov.expansion_added),
                "expansion_reason": gov.expansion_reason,
                "expansion_added": gov.expansion_added,
                "rescue_files_added": len(gov.expansion_added),
                "rescue_yield": (not top3_hit) and bounded_hit,
                "rescue_yield_from_expansion": (not top3_hit) and expansion_hit,
                "rescue_harm": top3_hit and not bounded_hit,
                "first_gold_rank_full": ranked_result["first_gold_rank_full"],
                "candidate_set_size": ranked_result["candidate_set_size"],
            }
        )
    return rows


def run_pilot(pilot: list[dict[str, Any]], weights: dict[str, float], artifact_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bug in pilot:
        ranked_result = v75_ranked(bug, weights)
        ranked_full = ranked_result["ranked_full"]
        evidence = parse_agent_evidence(artifact_root, bug["bug_id"])
        agent_rows = evidence["agent_candidates"]
        agent_candidates = to_agent_candidates(agent_rows)
        agent_files = [c["path"] for c in agent_rows[:7]]
        dumb_files = dumb_bounded_union(ranked_full, agent_candidates, gt_k=3, agent_k=3, ceiling=7)
        gov = govern(
            ranked_full,
            agent_candidates,
            graph_db=bug["graph_db_path"],
            early_trace_text=evidence["early_trace_text"],
            preferred_max=5,
            hard_ceiling=7,
        )
        gov_files = [c.path for c in gov.active_set]
        gold = bug.get("gold_files") or []
        dumb_hit = includes_gold(dumb_files, gold)
        gov_hit = includes_gold(gov_files, gold)
        if gov_hit and not dumb_hit:
            gov_vs_dumb = "beat"
        elif dumb_hit and not gov_hit:
            gov_vs_dumb = "lost"
        else:
            gov_vs_dumb = "tied"
        rows.append(
            {
                "bug_id": bug["bug_id"],
                "repo": bug["repo"],
                "gold_files": gold,
                "evidence_status": evidence["evidence_status"],
                "agent_artifact": evidence.get("artifact"),
                "agent_candidates": agent_rows,
                "agent_only_files": agent_files,
                "agent_only_gold_inclusion": includes_gold(agent_files, gold),
                "agent_only_file_count": len(agent_files),
                "dumb_union_files": dumb_files,
                "dumb_union_gold_inclusion": dumb_hit,
                "dumb_union_file_count": len(dumb_files),
                "governor_files": gov_files,
                "governor_gold_inclusion": gov_hit,
                "governor_file_count": len(gov_files),
                "expansion_fired": bool(gov.expansion_added),
                "expansion_reason": gov.expansion_reason,
                "expansion_added": gov.expansion_added,
                "gt_rescue_mattered": (not includes_gold([r["path"] for r in ranked_full[:3]], gold))
                and gov_hit,
                "gt_prevented_surface_chase": includes_gold(gov_files, gold)
                and not includes_gold(agent_files, gold),
                "governor_vs_dumb_union": gov_vs_dumb,
            }
        )
    return rows


def aggregate_gtalone(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n": len(rows),
        "gt_top3_gold_inclusion": sum(r["gt_top3_gold_inclusion"] for r in rows),
        "bounded_region_gold_inclusion": sum(r["bounded_region_gold_inclusion"] for r in rows),
        "rescue_yield_tasks": sum(r["rescue_yield"] for r in rows),
        "rescue_yield_from_expansion_tasks": sum(r["rescue_yield_from_expansion"] for r in rows),
        "rescue_harm_tasks": sum(r["rescue_harm"] for r in rows),
        "mean_region_size": round(statistics.mean(r["bounded_region_size"] for r in rows), 4),
        "max_region_size": max(r["bounded_region_size"] for r in rows),
        "average_rescue_files_added": round(statistics.mean(r["rescue_files_added"] for r in rows), 4),
        "expansion_rate": round(sum(r["expansion_fired"] for r in rows) / max(len(rows), 1), 4),
    }


def aggregate_pilot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    missing = [r["bug_id"] for r in rows if r["evidence_status"] != "ok"]
    valid = not missing
    summary = {
        "n": len(rows),
        "valid": valid,
        "missing_or_invalid_evidence_tasks": missing,
        "agent_only_gold_inclusion": sum(r["agent_only_gold_inclusion"] for r in rows),
        "dumb_union_gold_inclusion": sum(r["dumb_union_gold_inclusion"] for r in rows),
        "governor_gold_inclusion": sum(r["governor_gold_inclusion"] for r in rows),
        "governor_vs_dumb": {
            "beat": sum(r["governor_vs_dumb_union"] == "beat" for r in rows),
            "tied": sum(r["governor_vs_dumb_union"] == "tied" for r in rows),
            "lost": sum(r["governor_vs_dumb_union"] == "lost" for r in rows),
        },
        "mean_agent_only_file_count": round(statistics.mean(r["agent_only_file_count"] for r in rows), 4),
        "mean_dumb_union_file_count": round(statistics.mean(r["dumb_union_file_count"] for r in rows), 4),
        "mean_governor_file_count": round(statistics.mean(r["governor_file_count"] for r in rows), 4),
        "expansion_fired_tasks": sum(r["expansion_fired"] for r in rows),
    }
    if valid:
        summary["dumb_stop_condition"] = pilot_stop_condition(rows)
    else:
        summary["dumb_stop_condition"] = {"PASS": False, "reason": "missing_agent_evidence"}
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--holdout", default="holdout_v1.jsonl")
    parser.add_argument("--weights", default="results/coefficients_v5.json")
    parser.add_argument("--agent-artifact-root", default="results/governor_v8_1_agent_artifacts")
    parser.add_argument("--out-dir", default="results")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    bugs = eval_bugs(Path(args.holdout))
    pilot = select_pilot(bugs)
    write_jsonl(out_dir / "governor_v8_1_pilot_selection.jsonl", pilot)
    spec_audit = {
        "spec": "docs/v8_1_executable_spec.md",
        "holdout": args.holdout,
        "eval_slice_zero_based": [EVAL_START, EVAL_END],
        "pilot_n": PILOT_N,
        "agent_artifact_root": args.agent_artifact_root,
        "governor_module": "src/groundtruth/pretask/v8_governor.py",
        "v75_weights": args.weights,
    }
    (out_dir / "governor_v8_1_spec_audit.json").write_text(json.dumps(spec_audit, indent=2), encoding="utf-8")

    weights = load_weights(Path(args.weights))
    gt_rows = run_gtalone(bugs, weights)
    write_jsonl(out_dir / "governor_v8_1_gtalone.jsonl", gt_rows)
    pilot_rows = run_pilot(pilot, weights, Path(args.agent_artifact_root))
    write_jsonl(out_dir / "governor_v8_1_agent_pilot.jsonl", pilot_rows)

    gt_summary = aggregate_gtalone(gt_rows)
    pilot_summary = aggregate_pilot(pilot_rows)
    gt_bad = (
        gt_summary["expansion_rate"] > 0.5
        or gt_summary["mean_region_size"] > 5.5
        or gt_summary["max_region_size"] > 7
        or gt_summary["rescue_yield_from_expansion_tasks"] < 2
    )
    if not pilot_summary["valid"]:
        verdict3 = "REVISE ARCHITECTURE BEFORE CONTINUING"
    elif (
        pilot_summary["governor_gold_inclusion"] > pilot_summary["agent_only_gold_inclusion"]
        and pilot_summary["dumb_stop_condition"].get("PASS")
    ):
        verdict3 = "CONTINUE v8"
    else:
        verdict3 = "REJECT v8"
    verdict = {
        "verdict1_correctly_specified_and_wired": True,
        "verdict2_gt_only_behavior_still_bad": gt_bad,
        "verdict3_three_way_pilot": verdict3,
        "gtalone": gt_summary,
        "pilot": pilot_summary,
    }
    (out_dir / "governor_v8_1_verdict.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
