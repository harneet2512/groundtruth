from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

FILE_RE = re.compile(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9_]+")


def iter_task_dirs(root: Path) -> list[Path]:
    return sorted([p for p in root.iterdir() if p.is_dir()])


def load_trajectory(task_dir: Path) -> dict[str, Any]:
    p = task_dir / "trajectory.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    # SWE-agent writes <instance_id>.traj instead of trajectory.json.
    # The schema is compatible: top-level dict with a `trajectory` key whose
    # value is the list of step dicts (each with action/observation/response).
    for cand in sorted(task_dir.glob("*.traj")):
        return json.loads(cand.read_text(encoding="utf-8"))
    return {}


def steps(traj_obj: dict[str, Any]) -> list[dict[str, Any]]:
    raw = traj_obj.get("trajectory", [])
    return [s for s in raw if isinstance(s, dict)]


def first_actions(step_list: list[dict[str, Any]], n: int = 3) -> list[str]:
    return [str(s.get("action", "")) for s in step_list[:n]]


def collect_file_mentions(text: str) -> set[str]:
    return set(FILE_RE.findall(text or ""))


def find_brief_text(task_dir: Path, traj_obj: dict[str, Any]) -> str:
    sidecar = task_dir / "gt_brief.txt"
    if sidecar.exists():
        return sidecar.read_text(encoding="utf-8", errors="replace")
    for s in steps(traj_obj)[:2]:
        for key in ("response", "observation", "action"):
            v = str(s.get(key, ""))
            if "<gt-task-brief>" in v:
                return v
    return ""


def count_gt_query_calls(task_dir: Path, traj_obj: dict[str, Any]) -> int:
    sidecar = task_dir / "gt_query_calls.jsonl"
    if sidecar.exists():
        return sum(1 for ln in sidecar.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip())
    c = 0
    for s in steps(traj_obj):
        tool = str(s.get("tool", ""))
        action = str(s.get("action", ""))
        if tool == "gt_query" or "gt_query" in action:
            c += 1
    return c


def list_evidence_texts(task_dir: Path) -> list[str]:
    evd = task_dir / "gt_evidence"
    if not evd.exists():
        return []
    out: list[str] = []
    for p in sorted(evd.glob("edit_*.json")):
        try:
            out.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return out


def token_set(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_./-]{2,}", text.lower()))


def overlap_ratio(a: str, b: str) -> float:
    ta = token_set(a)
    tb = token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta))
