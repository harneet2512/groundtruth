"""TEG-1 metric extractor — adds hook-fire telemetry on top of paranoid metrics.

Outputs per-arm, per-task and aggregate JSON. Designed to be run on the host
after a TEG-1 arm completes. Run dir layout:

    <run_dir>/
      SWE-bench-Live/SWE-bench-Live/CodeActAgent/<llm>_maxiter_<N>_<run_name>/
        output.jsonl
      gt_logs/
        <iid>_teg1.jsonl         # commit-prompt hook event log
        <iid>_teg1.state.json    # hook end-of-task state
        <iid>_teg1.gold_paths.txt
        <iid>_brief.txt          # V1R-map brief

For each task we compute:

  Localization-to-action:
    steps_to_first_gold_touch
    steps_to_first_gold_edit
    touch_to_edit_gap
    wasted_reads_before_first_gold_touch
    wasted_edits_before_first_gold_edit
    edit_precision
    gold_edit_coverage

  Efficiency:
    total_turns
    turns_before_first_material_edit
    total_files_opened
    total_files_edited
    fraction_hitting_turn_cap (per arm aggregate)

  Failure pattern (per fixed terminology — see last_mile.md 2026-05-03):
    brief_miss          (gold not in pretask brief)
    edit_miss           (no gold file edited)
    resolve_miss        (no resolved patch)
    surface_chase_edits
    wrong_area_edits

  TEG-1 hook telemetry:
    teg1_fired                (bool)
    reads_at_fire             (int — what reads_seen was when fire happened)
    edits_at_fire
    fire_to_first_gold_edit   (steps from fire to first gold edit; null if no fire or no edit)
    edited_after_fire_in_brief
    surface_chase_after_fire

Usage:
    python3 teg1_metrics.py --run-dir /home/Lenovo/results/teg1_..._tier2_<ts> \
                            --gold-map /home/Lenovo/v1r_experiment/gold_map.json \
                            --out /home/Lenovo/results/teg1_metrics.json

The gold-map JSON maps instance_id -> list[str] of gold file paths. For Tier 1
this is the existing 15-task gold map; for Tier 2 it must be regenerated from
the SWE-bench-Live patch dataset before running this extractor.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any


def _norm(p: str) -> str:
    return p.replace("\\", "/").strip("/")


def _is_gold(path: str, gold: list[str]) -> bool:
    if not path or not gold:
        return False
    p = _norm(path)
    p_tail = "/".join(p.split("/")[-2:]) if "/" in p else p
    for g in gold:
        gn = _norm(g)
        if p == gn:
            return True
        if gn.endswith("/" + p) or p.endswith("/" + gn):
            return True
        gt = "/".join(gn.split("/")[-2:]) if "/" in gn else gn
        if p_tail == gt and "/" in p_tail:
            return True
    return False


def _is_scaffolding(path: str) -> bool:
    """Repo-root reproduce/test scaffolding — surface chase signal."""
    if not path:
        return False
    p = _norm(path)
    if "/" not in p:
        # repo-root file
        bn = p.lower()
        if bn.startswith("reproduce") or bn.startswith("test_repro") or bn.startswith("repro"):
            return True
        if bn in ("test.py", "tests.py", "main.py") and not p.startswith("src/"):
            return True
    return False


def _classify_event(ev: dict[str, Any]) -> tuple[str, str]:
    """Return ('read'|'edit'|'other', file_path)."""
    args = ev.get("args") or ev.get("action_args") or {}
    cmd = ev.get("command") or args.get("command") or ""
    path = args.get("path") or args.get("file_path") or ""
    ev_kind = (ev.get("action") or ev.get("event_type") or ev.get("name") or "").lower()
    if "edit" in ev_kind or "create" in ev_kind or "write" in ev_kind or "str_replace" in ev_kind:
        return "edit", path
    if "view" in ev_kind or "read" in ev_kind or "open" in ev_kind:
        return "read", path
    return "other", path


@dataclass
class TaskMetrics:
    iid: str
    total_turns: int = 0
    first_gold_touch_step: int | None = None
    first_gold_edit_step: int | None = None
    wasted_reads_before_first_gold_touch: int = 0
    wasted_edits_before_first_gold_edit: int = 0
    edit_precision: float = 0.0
    gold_edit_coverage: float = 0.0
    total_files_opened: int = 0
    total_files_edited: int = 0
    edit_count: int = 0
    read_count: int = 0
    turns_before_first_material_edit: int | None = None
    hit_turn_cap: bool = False
    any_gold_edited: bool = False
    surface_chase_edits: int = 0
    wrong_area_edits: int = 0
    has_patch: bool = False
    resolved: bool = False
    brief_miss: bool = False
    edit_miss: bool = False
    resolve_miss: bool = False
    teg1_fired: bool = False
    teg1_reads_at_fire: int | None = None
    teg1_edits_at_fire: int | None = None
    teg1_fire_to_first_gold_edit: int | None = None
    teg1_edited_after_fire_in_brief: bool = False
    teg1_surface_chase_after_fire: int = 0
    brief_files: list[str] = field(default_factory=list)
    gold_files: list[str] = field(default_factory=list)

    @property
    def touch_to_edit_gap(self) -> int | None:
        if self.first_gold_touch_step is None or self.first_gold_edit_step is None:
            return None
        return self.first_gold_edit_step - self.first_gold_touch_step


def extract_task(record: dict[str, Any], gold: list[str], gt_logs_dir: str) -> TaskMetrics:
    iid = record.get("instance_id", "")
    m = TaskMetrics(iid=iid, gold_files=list(gold))

    history = record.get("history") or record.get("trajectory") or []
    files_opened: set[str] = set()
    files_edited: set[str] = set()
    surface_chase_steps: list[int] = []

    for step, ev in enumerate(history):
        kind, path = _classify_event(ev)
        if not path:
            continue
        norm = _norm(path)
        if kind == "read":
            files_opened.add(norm)
            m.read_count += 1
            if _is_gold(norm, gold):
                if m.first_gold_touch_step is None:
                    m.first_gold_touch_step = step
            else:
                if m.first_gold_touch_step is None:
                    m.wasted_reads_before_first_gold_touch += 1
        elif kind == "edit":
            files_edited.add(norm)
            m.edit_count += 1
            if m.turns_before_first_material_edit is None:
                m.turns_before_first_material_edit = step
            if _is_gold(norm, gold):
                m.any_gold_edited = True
                if m.first_gold_edit_step is None:
                    m.first_gold_edit_step = step
            else:
                if m.first_gold_edit_step is None:
                    m.wasted_edits_before_first_gold_edit += 1
                if _is_scaffolding(norm):
                    m.surface_chase_edits += 1
                    surface_chase_steps.append(step)
                else:
                    m.wrong_area_edits += 1

    m.total_turns = len(history)
    m.total_files_opened = len(files_opened)
    m.total_files_edited = len(files_edited)
    m.hit_turn_cap = m.total_turns >= 198
    if m.edit_count:
        m.edit_precision = round(
            sum(1 for f in files_edited if _is_gold(f, gold)) / m.edit_count, 3
        )
    if gold:
        m.gold_edit_coverage = round(
            sum(1 for g in gold if any(_is_gold(_norm(g), [_norm(f)]) or _norm(g) == _norm(f) for f in files_edited)) / len(gold),
            3,
        )

    m.has_patch = bool(record.get("test_result", {}).get("git_patch") or record.get("model_patch"))
    m.resolved = bool(record.get("report", {}).get("resolved")) or bool(record.get("resolved"))

    brief_path = os.path.join(gt_logs_dir, f"{iid}_brief.txt")
    if os.path.exists(brief_path):
        brief_text = open(brief_path, "r", encoding="utf-8", errors="replace").read()
        m.brief_files = re.findall(r"^\s*\d+\.\s+(\S+)", brief_text, re.MULTILINE)
        m.brief_miss = not any(_is_gold(_norm(f), gold) for f in m.brief_files)
    else:
        m.brief_files = []
        m.brief_miss = True

    m.edit_miss = not m.any_gold_edited
    m.resolve_miss = not m.resolved

    teg1_log = os.path.join(gt_logs_dir, f"{iid}_teg1.jsonl")
    if os.path.exists(teg1_log):
        for line in open(teg1_log, "r", encoding="utf-8", errors="replace"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "fire":
                m.teg1_fired = True
                m.teg1_reads_at_fire = rec.get("reads_seen")
                m.teg1_edits_at_fire = rec.get("edits_seen")
                if m.first_gold_edit_step is not None and m.teg1_reads_at_fire is not None:
                    fire_step = m.teg1_reads_at_fire + (m.teg1_edits_at_fire or 0)
                    m.teg1_fire_to_first_gold_edit = max(0, m.first_gold_edit_step - fire_step)
                m.teg1_edited_after_fire_in_brief = m.any_gold_edited
                m.teg1_surface_chase_after_fire = sum(1 for s in surface_chase_steps if s > (m.teg1_reads_at_fire or 0))
                break

    return m


def aggregate(rows: list[TaskMetrics]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}

    def median(vals: list[Any]) -> float | None:
        v = sorted(x for x in vals if x is not None)
        if not v:
            return None
        m = len(v) // 2
        return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2

    def mean(vals: list[Any]) -> float | None:
        v = [x for x in vals if x is not None]
        return round(sum(v) / len(v), 3) if v else None

    return {
        "n": n,
        "resolved_count": sum(1 for r in rows if r.resolved),
        "brief_miss": sum(1 for r in rows if r.brief_miss),
        "edit_miss": sum(1 for r in rows if r.edit_miss),
        "resolve_miss": sum(1 for r in rows if r.resolve_miss),
        "first_gold_touch_step_median": median([r.first_gold_touch_step for r in rows]),
        "first_gold_touch_step_mean": mean([r.first_gold_touch_step for r in rows]),
        "first_gold_edit_step_median": median([r.first_gold_edit_step for r in rows]),
        "first_gold_edit_step_mean": mean([r.first_gold_edit_step for r in rows]),
        "touch_to_edit_gap_median": median([r.touch_to_edit_gap for r in rows]),
        "touch_to_edit_gap_mean": mean([r.touch_to_edit_gap for r in rows]),
        "wasted_edits_before_first_gold_edit_mean": mean([r.wasted_edits_before_first_gold_edit for r in rows]),
        "edit_precision_mean": mean([r.edit_precision for r in rows]),
        "gold_edit_coverage_mean": mean([r.gold_edit_coverage for r in rows]),
        "surface_chase_edits_total": sum(r.surface_chase_edits for r in rows),
        "wrong_area_edits_total": sum(r.wrong_area_edits for r in rows),
        "hit_turn_cap_count": sum(1 for r in rows if r.hit_turn_cap),
        "total_turns_mean": mean([r.total_turns for r in rows]),
        "teg1_fired_count": sum(1 for r in rows if r.teg1_fired),
        "teg1_fire_to_first_gold_edit_median": median([r.teg1_fire_to_first_gold_edit for r in rows if r.teg1_fired]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--gold-map", required=True, help="JSON: instance_id -> [gold paths]")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    gold_map = json.load(open(args.gold_map, "r", encoding="utf-8"))

    output_jsonl = ""
    for root, _, files in os.walk(args.run_dir):
        if "output.jsonl" in files and "/CodeActAgent/" in root.replace("\\", "/"):
            output_jsonl = os.path.join(root, "output.jsonl")
            break
    if not output_jsonl:
        print(f"ERROR: no output.jsonl under {args.run_dir}")
        return 2

    gt_logs_dir = os.path.join(args.run_dir, "gt_logs")
    rows: list[TaskMetrics] = []
    for line in open(output_jsonl, "r", encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        iid = rec.get("instance_id", "")
        gold = gold_map.get(iid, [])
        rows.append(extract_task(rec, gold, gt_logs_dir))

    summary = {
        "run_dir": args.run_dir,
        "n_tasks": len(rows),
        "aggregate": aggregate(rows),
        "per_task": [asdict(r) for r in rows],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"Wrote {args.out}: n={len(rows)} resolved={summary['aggregate']['resolved_count']}")
    print(json.dumps(summary["aggregate"], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
