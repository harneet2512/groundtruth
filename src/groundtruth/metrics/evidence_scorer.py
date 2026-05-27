"""Phase 5: Behavioral metrics for GT evidence quality.

Three numbers replace fake preflight checks:
  evidence_precision: % of injections where content was factually correct
  evidence_recall:    % of gold-patch context that GT delivered
  agent_uptake:       % of correct injections where agent used the evidence

All computed post-run by comparing GT injections against the gold patch diff.
No LLM involved — pure string matching + graph.db validation.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InjectionRecord:
    layer: str
    file_path: str
    content: str
    markers: list[str]
    char_count: int
    target_function: str = ""
    confidence: float = 0.0


@dataclass
class EvidenceScore:
    precision: float = 0.0
    recall: float = 0.0
    uptake: float = 0.0
    total_injections: int = 0
    correct_injections: int = 0
    gold_contexts_needed: int = 0
    gold_contexts_delivered: int = 0
    uptake_opportunities: int = 0
    uptake_hits: int = 0
    details: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"precision={self.precision:.2f} recall={self.recall:.2f} "
            f"uptake={self.uptake:.2f} "
            f"({self.correct_injections}/{self.total_injections} correct, "
            f"{self.gold_contexts_delivered}/{self.gold_contexts_needed} recalled, "
            f"{self.uptake_hits}/{self.uptake_opportunities} used)"
        )


def parse_gold_patch(patch_text: str) -> list[dict]:
    """Extract edited files + function contexts from a unified diff."""
    files: list[dict] = []
    current_file = None
    current_functions: set[str] = set()

    for line in patch_text.splitlines():
        if line.startswith("diff --git"):
            if current_file:
                files.append({"file": current_file, "functions": list(current_functions)})
            current_file = None
            current_functions = set()
        elif line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@"):
            match = re.search(r"@@.*@@\s*(.*)", line)
            if match:
                ctx = match.group(1).strip()
                func_match = re.match(r"(?:def|class|function|fn)\s+(\w+)", ctx)
                if not func_match:
                    func_match = re.match(r"func\s+(?:\([^)]*\)\s+)?(\w+)", ctx)
                if func_match:
                    current_functions.add(func_match.group(1))

    if current_file:
        files.append({"file": current_file, "functions": list(current_functions)})
    return files


def parse_injections(gt_log_path: str) -> list[InjectionRecord]:
    """Parse GT injection log (JSONL format) into structured records."""
    records: list[InjectionRecord] = []
    if not os.path.exists(gt_log_path):
        return records
    with open(gt_log_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                markers = []
                content = obj.get("rendered_text", "") or obj.get("text", "")
                for m in ("[SIGNATURE]", "[TEST]", "[BEHAVIORAL CONTRACT]",
                          "[CONTRACT]", "[PEER]", "[OVERRIDE]", "[PATTERN]",
                          "[SIMILAR]", "[COMPLETENESS]", "PRESERVE:", "MUTATES:"):
                    if m in content:
                        markers.append(m)
                records.append(InjectionRecord(
                    layer=obj.get("layer", ""),
                    file_path=obj.get("file_path", "") or obj.get("file", ""),
                    content=content,
                    markers=markers,
                    char_count=len(content),
                    target_function=obj.get("symbol", "") or obj.get("function", ""),
                    confidence=float(obj.get("confidence", 0.0)),
                ))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return records


def compute_precision(
    injections: list[InjectionRecord],
    gold_files: list[dict],
    db_path: str = "",
) -> tuple[int, int, list[dict]]:
    """Precision: how many injections reference files/functions in the gold patch?"""
    gold_file_set = {g["file"] for g in gold_files}
    gold_func_set = {fn for g in gold_files for fn in g.get("functions", [])}
    correct = 0
    total = 0
    details: list[dict] = []

    for inj in injections:
        if not inj.content.strip():
            continue
        total += 1
        inj_norm = inj.file_path.replace("\\", "/").lstrip("/")
        def _path_suffix_match(a: str, b: str) -> bool:
            pa, pb = a.split("/"), b.split("/")
            shorter = pa if len(pa) <= len(pb) else pb
            longer = pb if len(pa) <= len(pb) else pa
            return longer[-len(shorter):] == shorter if shorter else False
        file_match = any(_path_suffix_match(inj_norm, gf) for gf in gold_file_set)
        func_match = inj.target_function in gold_func_set if inj.target_function else False

        is_correct = file_match or func_match

        if is_correct and db_path and os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                row = conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE name = ? AND file_path LIKE ?",
                    (inj.target_function, f"%{inj_norm.split('/')[-1]}"),
                ).fetchone()
                conn.close()
                if row and row[0] == 0 and inj.target_function:
                    is_correct = False
            except Exception:
                pass

        if is_correct:
            correct += 1
        details.append({
            "layer": inj.layer, "file": inj.file_path,
            "function": inj.target_function,
            "correct": is_correct, "markers": inj.markers,
        })
    return correct, total, details


def compute_recall(
    injections: list[InjectionRecord],
    gold_files: list[dict],
) -> tuple[int, int]:
    """Recall: how many gold-patch files/functions did GT deliver context for?"""
    delivered_files = {
        inj.file_path.replace("\\", "/").lstrip("/").split("/")[-1]
        for inj in injections if inj.content.strip()
    }
    delivered_funcs = {
        inj.target_function for inj in injections
        if inj.target_function and inj.content.strip()
    }

    needed = 0
    delivered = 0
    for g in gold_files:
        gold_base = g["file"].split("/")[-1]
        needed += 1
        if gold_base in delivered_files:
            delivered += 1
        for fn in g.get("functions", []):
            needed += 1
            if fn in delivered_funcs:
                delivered += 1

    return delivered, needed


def compute_uptake(
    injections: list[InjectionRecord],
    agent_actions: list[dict],
) -> tuple[int, int]:
    """Uptake: did the agent's actions reference files/functions GT injected?

    Scans ALL agent actions for references to injected file/function names.
    Not index-aligned — injections and actions are different event streams.
    """
    opportunities = 0
    hits = 0

    all_action_text = " ".join(
        (a.get("text", "") or a.get("content", "") or "").lower()
        for a in agent_actions
        if a.get("action_type", "") in ("FileEditAction", "CmdRunAction", "edit", "run")
    )

    for inj in injections:
        if not inj.content.strip():
            continue
        opportunities += 1
        used = False
        if inj.target_function and inj.target_function.lower() in all_action_text:
            used = True
        elif inj.file_path:
            fp_base = inj.file_path.split("/")[-1].lower()
            if fp_base and fp_base in all_action_text:
                used = True
        if used:
            hits += 1

    return hits, opportunities


def score_run(
    gt_log_path: str,
    gold_patch_path: str,
    agent_history_path: str = "",
    db_path: str = "",
) -> EvidenceScore:
    """Compute all 3 metrics for a single run."""
    with open(gold_patch_path, encoding="utf-8", errors="ignore") as f:
        gold_patch = f.read()
    gold_files = parse_gold_patch(gold_patch)

    injections = parse_injections(gt_log_path)

    correct, total, details = compute_precision(injections, gold_files, db_path)
    delivered, needed = compute_recall(injections, gold_files)

    hits, opportunities = 0, 0
    if agent_history_path and os.path.exists(agent_history_path):
        agent_actions: list[dict] = []
        with open(agent_history_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    agent_actions.append(json.loads(line.strip()))
                except (json.JSONDecodeError, ValueError):
                    continue
        hits, opportunities = compute_uptake(injections, agent_actions)

    score = EvidenceScore(
        precision=correct / total if total > 0 else 0.0,
        recall=delivered / needed if needed > 0 else 0.0,
        uptake=hits / opportunities if opportunities > 0 else 0.0,
        total_injections=total,
        correct_injections=correct,
        gold_contexts_needed=needed,
        gold_contexts_delivered=delivered,
        uptake_opportunities=opportunities,
        uptake_hits=hits,
        details=details,
    )
    return score


def score_run_from_artifacts(run_dir: str) -> EvidenceScore | None:
    """Score a run from standard artifact paths."""
    gt_log = os.path.join(run_dir, "gt_interactions.jsonl")
    if not os.path.exists(gt_log):
        gt_log = os.path.join(run_dir, "gt_interaction_log.jsonl")
    if not os.path.exists(gt_log):
        gt_log = os.path.join(run_dir, "gt_layer_events.jsonl")
    gold_patch = os.path.join(run_dir, "gold_patch.diff")
    if not os.path.exists(gold_patch):
        gold_patch = os.path.join(run_dir, "test_patch.diff")
    history = os.path.join(run_dir, "output.jsonl")
    db = os.path.join(run_dir, "graph.db")

    if not os.path.exists(gt_log) or not os.path.exists(gold_patch):
        return None

    return score_run(
        gt_log_path=gt_log,
        gold_patch_path=gold_patch,
        agent_history_path=history if os.path.exists(history) else "",
        db_path=db if os.path.exists(db) else "",
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Score GT evidence quality for a run")
    parser.add_argument("run_dir", help="Directory containing run artifacts")
    args = parser.parse_args()

    result = score_run_from_artifacts(args.run_dir)
    if result is None:
        print("Missing artifacts (need gt_interaction_log.jsonl + gold_patch.diff)")
    else:
        print(result.summary())
        print(json.dumps({
            "precision": result.precision,
            "recall": result.recall,
            "uptake": result.uptake,
            "total_injections": result.total_injections,
            "correct_injections": result.correct_injections,
            "gold_contexts_needed": result.gold_contexts_needed,
            "gold_contexts_delivered": result.gold_contexts_delivered,
            "uptake_opportunities": result.uptake_opportunities,
            "uptake_hits": result.uptake_hits,
        }, indent=2))
