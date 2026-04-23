#!/usr/bin/env python3
"""Followed Detector - post-run analyzer for GT steer follow-through.

Computes deterministic per-steer and per-task follow-through state from a
GT run archive. Replaces the broken in-hook `ack_followed` heuristic.

For each steer (keyed by `ack_id`), the detector derives:
  - delivered                 : `steer_delivered` emitted
  - engaged                   : agent did any action in cycles > armed_cycle
  - targeted_edit_after_steer : a later `material_edit` touched target file
  - verification_ran          : agent ran pytest OR opened the target file
  - ack_followed_raw          : hook emitted `ack_followed` for this ack_id

Per-task rollup adds:
  - submitted                 : preds.json has non-empty model_patch
  - any_targeted_edit         : at least one steer had targeted_edit_after_steer
  - any_verification          : agent ran pytest at any point OR opened a
                                recommended file

No hook changes. Pure post-run analysis. Runs locally on archive directories.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

PYTEST_CMDS = re.compile(r"\bpytest\b|\bpython\s+-m\s+pytest\b|\bunittest\b")
DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)


def _basename(p: str) -> str:
    if not p:
        return ""
    return p.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _suffix_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    ba, bb = _basename(a), _basename(b)
    return bool(ba) and ba == bb


@dataclass
class Steer:
    ack_id: str
    intervention_id: str
    armed_cycle: int
    armed_ts: str
    target_file: str
    target_kind: str
    target_text: str
    tier: str
    channel: str
    delivered: bool = False
    delivered_cycle: int | None = None
    engagement_classification: str | None = None
    engaged: bool = False
    targeted_edit_after_steer: bool = False
    targeted_edit_cycle: int | None = None
    targeted_edit_file: str | None = None
    verification_ran: bool = False
    verification_reason: str = ""
    ack_followed_raw: bool = False


@dataclass
class TaskResult:
    task_id: str
    arm: str
    run_id: str
    steers: list[Steer] = field(default_factory=list)
    submitted: bool = False
    patch_files: list[str] = field(default_factory=list)
    traj_steps: int = 0
    ran_any_pytest: bool = False
    any_opened_files: list[str] = field(default_factory=list)

    @property
    def delivered_count(self) -> int:
        return sum(1 for s in self.steers if s.delivered)

    @property
    def engaged_count(self) -> int:
        return sum(1 for s in self.steers if s.engaged)

    @property
    def targeted_edit_count(self) -> int:
        return sum(1 for s in self.steers if s.targeted_edit_after_steer)

    @property
    def verification_count(self) -> int:
        return sum(1 for s in self.steers if s.verification_ran)

    @property
    def ack_followed_raw_count(self) -> int:
        return sum(1 for s in self.steers if s.ack_followed_raw)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "arm": self.arm,
            "run_id": self.run_id,
            "submitted": self.submitted,
            "patch_files": self.patch_files,
            "traj_steps": self.traj_steps,
            "ran_any_pytest": self.ran_any_pytest,
            "steer_count": len(self.steers),
            "delivered_count": self.delivered_count,
            "engaged_count": self.engaged_count,
            "targeted_edit_count": self.targeted_edit_count,
            "verification_count": self.verification_count,
            "ack_followed_raw_count": self.ack_followed_raw_count,
            "any_targeted_edit": self.targeted_edit_count > 0,
            "steers": [asdict(s) for s in self.steers],
        }


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events


def _parse_trajectory(traj_path: Path) -> tuple[int, bool, list[str]]:
    """Return (step_count, ran_any_pytest, opened_files)."""
    if not traj_path.exists():
        return (0, False, [])
    try:
        data = json.loads(traj_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return (0, False, [])
    steps = data.get("trajectory") if isinstance(data, dict) else data
    if not isinstance(steps, list):
        return (0, False, [])
    ran_pytest = False
    opened: list[str] = []
    count = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action") or "").strip()
        if not action:
            continue
        count += 1
        if PYTEST_CMDS.search(action):
            ran_pytest = True
        for tok in action.split():
            if "/" in tok and "." in tok and not tok.startswith("-"):
                opened.append(tok.strip(",;'\""))
    return (count, ran_pytest, list(dict.fromkeys(opened)))


def _read_patch_files(preds_path: Path, task_id: str) -> tuple[bool, list[str]]:
    """Return (submitted, patch_files). Submitted iff model_patch non-empty."""
    if not preds_path.exists():
        return (False, [])
    try:
        preds = json.loads(preds_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return (False, [])
    rec = preds.get(task_id) if isinstance(preds, dict) else None
    if not isinstance(rec, dict):
        return (False, [])
    patch = (rec.get("model_patch") or "").strip()
    if not patch:
        return (False, [])
    files = [m.group(2) for m in DIFF_HEADER.finditer(patch)]
    return (True, files)


def analyze_task(task_dir: Path) -> TaskResult | None:
    """Build per-steer + per-task followed state from archive files."""
    task_id = task_dir.name
    tel_path = task_dir / "gt_hook_telemetry.jsonl"
    events = _read_jsonl(tel_path)
    if not events:
        return None

    arm = ""
    run_id = ""
    for ev in events:
        if ev.get("arm"):
            arm = ev["arm"]
            run_id = ev.get("run_id", "")
            break

    preds_path = task_dir / "preds.json"
    submitted, patch_files = _read_patch_files(preds_path, task_id)

    traj_path = task_dir / task_id / f"{task_id}.traj"
    traj_steps, ran_any_pytest, opened_files = _parse_trajectory(traj_path)

    steers_by_ack: dict[str, Steer] = {}
    last_cycle = 0
    for ev in events:
        cyc = int(ev.get("cycle", 0) or 0)
        if cyc > last_cycle:
            last_cycle = cyc
        if ev.get("event") == "ack_armed":
            ack_id = ev.get("ack_id", "")
            if not ack_id:
                continue
            ena = ev.get("expected_next_action") or {}
            steers_by_ack[ack_id] = Steer(
                ack_id=ack_id,
                intervention_id=ev.get("intervention_id", ""),
                armed_cycle=cyc,
                armed_ts=ev.get("ts", ""),
                target_file=ena.get("target", "") if isinstance(ena, dict) else "",
                target_kind=ena.get("kind", "") if isinstance(ena, dict) else "",
                target_text=ena.get("text", "") if isinstance(ena, dict) else "",
                tier=ev.get("tier", "") or ev.get("confidence_tier", ""),
                channel=ev.get("channel", ""),
            )

    for ev in events:
        et = ev.get("event", "")
        ack_id = ev.get("ack_id", "")
        if not ack_id or ack_id not in steers_by_ack:
            continue
        s = steers_by_ack[ack_id]
        cyc = int(ev.get("cycle", 0) or 0)
        if et == "steer_delivered":
            s.delivered = True
            s.delivered_cycle = cyc
        elif et == "ack_engagement":
            cls = ev.get("classification", "")
            s.engagement_classification = cls
            if cls and cls not in ("no_visible_engagement",):
                s.engaged = True
        elif et == "ack_followed":
            s.ack_followed_raw = True

    material_edits: list[tuple[int, list[str]]] = []
    for ev in events:
        if ev.get("event") == "material_edit":
            cyc = int(ev.get("cycle", 0) or 0)
            files = ev.get("files") or []
            if isinstance(files, list):
                material_edits.append((cyc, [str(f) for f in files]))

    for s in steers_by_ack.values():
        if not s.target_file:
            continue
        if not s.delivered or s.delivered_cycle is None:
            continue
        for mc, mfiles in material_edits:
            if mc < s.delivered_cycle:
                continue
            hit = next((f for f in mfiles if _suffix_match(s.target_file, f)), None)
            if hit:
                s.targeted_edit_after_steer = True
                s.targeted_edit_cycle = mc
                s.targeted_edit_file = hit
                break

    cycle_timestamps = sorted({int(ev.get("cycle", 0) or 0) for ev in events if ev.get("event") == "cycle"})
    for s in steers_by_ack.values():
        if s.engaged or s.delivered_cycle is None:
            continue
        if any(c > s.delivered_cycle for c in cycle_timestamps):
            s.engaged = True
            if s.engagement_classification is None:
                s.engagement_classification = "derived_from_subsequent_cycle"

    for s in steers_by_ack.values():
        if not s.delivered:
            continue
        if ran_any_pytest:
            s.verification_ran = True
            s.verification_reason = "ran_pytest_at_task_level"
            continue
        if s.target_file and any(_suffix_match(s.target_file, op) for op in opened_files):
            s.verification_ran = True
            s.verification_reason = "target_file_opened_in_traj"

    return TaskResult(
        task_id=task_id,
        arm=arm,
        run_id=run_id,
        steers=sorted(steers_by_ack.values(), key=lambda x: x.armed_cycle),
        submitted=submitted,
        patch_files=patch_files,
        traj_steps=traj_steps,
        ran_any_pytest=ran_any_pytest,
        any_opened_files=opened_files[:50],
    )


def analyze_archive(archive: Path) -> dict:
    tasks: list[TaskResult] = []
    for item in sorted(archive.iterdir()):
        if not item.is_dir():
            continue
        if not item.name.startswith(
            ("astropy__", "django__", "sympy__", "pylint__", "sphinx__",
             "requests__", "scikit-learn__", "flask__", "matplotlib__",
             "seaborn__", "pytest-dev__")
        ):
            continue
        t = analyze_task(item)
        if t is None:
            continue
        tasks.append(t)

    total_steers = sum(len(t.steers) for t in tasks)
    total_delivered = sum(t.delivered_count for t in tasks)
    total_engaged = sum(t.engaged_count for t in tasks)
    total_targeted = sum(t.targeted_edit_count for t in tasks)
    total_verification = sum(t.verification_count for t in tasks)
    total_ack_followed_raw = sum(t.ack_followed_raw_count for t in tasks)

    def _rate(hits: int, total: int) -> float | None:
        return (hits / total) if total > 0 else None

    return {
        "archive": str(archive),
        "task_count": len(tasks),
        "total_steers": total_steers,
        "total_delivered": total_delivered,
        "total_engaged": total_engaged,
        "total_targeted_edit": total_targeted,
        "total_verification_ran": total_verification,
        "total_ack_followed_raw": total_ack_followed_raw,
        "delivered_rate": _rate(total_delivered, total_steers),
        "engaged_rate": _rate(total_engaged, total_delivered),
        "targeted_edit_rate": _rate(total_targeted, total_delivered),
        "verification_rate": _rate(total_verification, total_delivered),
        "ack_followed_raw_rate": _rate(total_ack_followed_raw, total_delivered),
        "tasks_submitted": sum(1 for t in tasks if t.submitted),
        "tasks_any_targeted_edit": sum(1 for t in tasks if t.targeted_edit_count > 0),
        "tasks_ran_pytest": sum(1 for t in tasks if t.ran_any_pytest),
        "tasks": [t.to_dict() for t in tasks],
    }


def render_markdown(agg: dict, per_steer: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"### {Path(agg['archive']).name}")
    lines.append("")
    lines.append(f"- **Tasks:** {agg['task_count']}")
    lines.append(f"- **Total steers:** {agg['total_steers']}")
    lines.append(f"- **Delivered:** {agg['total_delivered']} (rate {agg['delivered_rate']})")
    lines.append(f"- **Engaged:** {agg['total_engaged']} (rate over delivered {agg['engaged_rate']})")
    lines.append(
        f"- **Targeted edit after steer:** {agg['total_targeted_edit']} "
        f"(rate over delivered {agg['targeted_edit_rate']})"
    )
    lines.append(
        f"- **Verification ran:** {agg['total_verification_ran']} "
        f"(rate over delivered {agg['verification_rate']})"
    )
    lines.append(
        f"- **ack_followed (hook, raw):** {agg['total_ack_followed_raw']} "
        f"(rate over delivered {agg['ack_followed_raw_rate']})"
    )
    lines.append(f"- **Tasks submitted:** {agg['tasks_submitted']} / {agg['task_count']}")
    lines.append(f"- **Tasks with any targeted edit:** {agg['tasks_any_targeted_edit']} / {agg['task_count']}")
    lines.append(f"- **Tasks ran pytest:** {agg['tasks_ran_pytest']} / {agg['task_count']}")
    lines.append("")
    lines.append("**Per-task:**")
    lines.append("")
    lines.append("| task | arm | steers | deliv | engaged | targeted | verif | ack_raw | submitted |")
    lines.append("|---|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|")
    for t in agg["tasks"]:
        lines.append(
            f"| {t['task_id']} | {t['arm']} | {t['steer_count']} | "
            f"{t['delivered_count']} | {t['engaged_count']} | "
            f"{t['targeted_edit_count']} | {t['verification_count']} | "
            f"{t['ack_followed_raw_count']} | {'YES' if t['submitted'] else 'no'} |"
        )
    if per_steer:
        lines.append("")
        lines.append("**Per-steer:**")
        lines.append("")
        lines.append("| task | cyc | ack_id | tier | target | kind | deliv | eng | targ_edit | verif | ack_raw |")
        lines.append("|---|:-:|---|---|---|---|:-:|:-:|:-:|:-:|:-:|")
        for t in agg["tasks"]:
            for s in t["steers"]:
                tgt = _basename(s["target_file"]) or "—"
                lines.append(
                    f"| {t['task_id']} | {s['armed_cycle']} | {s['ack_id']} | "
                    f"{s['tier']} | {tgt} | {s['target_kind']} | "
                    f"{'Y' if s['delivered'] else '-'} | {'Y' if s['engaged'] else '-'} | "
                    f"{'Y' if s['targeted_edit_after_steer'] else '-'} | "
                    f"{'Y' if s['verification_ran'] else '-'} | "
                    f"{'Y' if s['ack_followed_raw'] else '-'} |"
                )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Followed Detector - post-run analyzer for GT steer follow-through")
    ap.add_argument("archives", nargs="+", help="Archive directories")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    ap.add_argument("--per-steer", action="store_true", help="Include per-steer timeline in markdown")
    args = ap.parse_args()

    results: list[dict] = []
    for a in args.archives:
        p = Path(a).resolve()
        if not p.is_dir():
            print(f"SKIP (not a directory): {p}", file=sys.stderr)
            continue
        results.append(analyze_archive(p))

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return 0

    for r in results:
        print(render_markdown(r, per_steer=args.per_steer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
