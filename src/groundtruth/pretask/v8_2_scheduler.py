"""Trace-gated v8.2 scheduler experiment.

This module is intentionally separate from the frozen static v8 governor.  It
consumes frozen v7.5 ranked records plus parsed early trace events and returns a
bounded active working set.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from groundtruth.pretask.v8_governor import (
    AgentCandidate,
    agent_path_allowed,
    includes_gold,
    normalize_path,
)


TRACE_FIELDS = ("action", "observation", "response", "content", "message", "tool", "args")
EARLY_ACTION_STEPS = 12
TIER_BY_SIGNAL = {
    "first_edit": 4,
    "material_edit": 4,
    "diff_header": 4,
    "stack_trace": 3,
    "failing_test": 3,
    "tool_output_path": 3,
    "repeated_open_search": 2,
    "single_open_search": 1,
}
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
_SKIP_LINE_RE = re.compile(
    r"^\s*(?:"
    r'["\x60]?(?:github\.com|golang\.org|google\.golang\.org|k8s\.io|sigs\.k8s\.io|gopkg\.in)[/"]'
    r"|package\s+\w+"
    r'|import\s*[\("]'
    r"|\t+\"[a-z]"
    r")",
)
PY_FRAME_RE = re.compile(r'File "([^"]+)", line \d+')
V8_FRAME_RE = re.compile(r"\bat\s+[^(]+\(([^():]+(?:/[^():]+)+):\d+:\d+\)")


@dataclass(frozen=True)
class TraceEvent:
    path: str
    signal: str
    tier: int
    step: int


@dataclass(frozen=True)
class AgentFileEvidence:
    path: str
    tier: int
    event_count: int
    first_step: int
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScheduledFile:
    path: str
    tier: int
    gt_score: float
    event_count: int
    first_step: int
    reason: str
    has_gt_support: bool
    edge_confidence: float = 0.0
    anchor_path: str | None = None


@dataclass(frozen=True)
class ScheduleResult:
    active_files: list[str]
    active_set: list[ScheduledFile]
    dropped_files: list[str]
    structural_added: list[str]
    provisional_gt_anchors: list[str]


@dataclass(frozen=True)
class TraceParseResult:
    status: str
    artifact: str | None
    events: list[TraceEvent] = field(default_factory=list)
    agent_files: dict[str, AgentFileEvidence] = field(default_factory=dict)
    early_trace_text: str = ""
    action_steps: int = 0
    error: str | None = None


def _gt_score(record: dict[str, Any] | None) -> float:
    if not record:
        return 0.0
    try:
        return float(record.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def structurally_eligible(record: dict[str, Any]) -> bool:
    """Same eligibility rule as static v8 rescue admission."""
    comps = record.get("components") or {}
    entered_via = str(record.get("entered_via") or "")
    try:
        reach = float(comps.get("reach", 0.0) or 0.0)
    except (TypeError, ValueError):
        reach = 0.0
    try:
        anchor_prox = float(comps.get("anchor_prox", 0.0) or 0.0)
    except (TypeError, ValueError):
        anchor_prox = 0.0
    try:
        path_len = int(record.get("min_path_length_from_anchor", 999) or 999)
    except (TypeError, ValueError):
        path_len = 999

    if not (entered_via in {"graph_rescue", "both"} or anchor_prox >= 0.333):
        return False
    if path_len > 2:
        return False
    if not (reach >= 0.35 or anchor_prox >= 0.333):
        return False
    return not (reach == 0 and anchor_prox == 0 and entered_via == "semantic_seed")


def _path_allowed(path: str, signal: str) -> bool:
    p = normalize_path(path)
    if not p or any(part in f"/{p}" for part in EXCLUDED_PATH_PARTS):
        return False
    if signal in {"single_open_search", "repeated_open_search"}:
        return agent_path_allowed(p, "generic_open")
    return agent_path_allowed(p, "command_output")


def _event_text(step: Any) -> str:
    if not isinstance(step, dict):
        return str(step)
    chunks: list[str] = []
    for key in TRACE_FIELDS:
        value = step.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            import json

            chunks.append(json.dumps(value, sort_keys=True))
        else:
            chunks.append(str(value))
    return "\n".join(chunks)


def _action_bearing(step: Any) -> bool:
    return isinstance(step, dict) and any(step.get(k) for k in ("action", "tool", "args"))


def _paths_for_signal(text: str, signal: str) -> list[str]:
    paths: list[str] = []
    if signal == "diff_header":
        for match in DIFF_RE.finditer(text):
            paths.append(normalize_path(match.group("b")))
    elif signal == "stack_trace":
        for line in text.split("\n"):
            if len(line) > 2000:
                continue
            for regex in (PY_FRAME_RE, V8_FRAME_RE):
                for match in regex.finditer(line):
                    paths.append(normalize_path(match.group(1)))
    else:
        for line in text.split("\n"):
            if len(line) > 2000 or _SKIP_LINE_RE.match(line):
                continue
            for match in PATH_RE.finditer(line):
                paths.append(normalize_path(match.group("path")))

    out: list[str] = []
    for path in paths:
        if path and path not in out and _path_allowed(path, signal):
            out.append(path)
    return out


def _raw_trace_steps(raw: Any) -> list[Any] | None:
    if isinstance(raw, dict):
        steps = raw.get("trajectory")
        if steps is None:
            steps = raw.get("history")
        return steps if isinstance(steps, list) else None
    return raw if isinstance(raw, list) else None


def find_trace_artifact(root: Path, bug_id: str) -> Path | None:
    for rel in (
        Path(bug_id) / "trajectory.traj",
        Path(bug_id) / "trajectory.json",
        Path(f"{bug_id}.traj"),
        Path(f"{bug_id}.json"),
    ):
        path = root / rel
        if path.exists():
            return path
    return None


def parse_trace_artifact(root: Path, bug_id: str) -> TraceParseResult:
    import json

    artifact = find_trace_artifact(root, bug_id)
    if artifact is None:
        return TraceParseResult(status="missing", artifact=None)
    try:
        raw = json.loads(artifact.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return TraceParseResult(status="unreadable", artifact=str(artifact), error=str(exc))
    steps = _raw_trace_steps(raw)
    if steps is None:
        return TraceParseResult(status="invalid_schema", artifact=str(artifact))

    events: list[TraceEvent] = []
    trace_chunks: list[str] = []
    open_search_steps: dict[str, set[int]] = {}
    action_steps = 0
    saw_first_edit = False
    for step_idx, step in enumerate(steps):
        if not _action_bearing(step):
            continue
        action_steps += 1
        text = _event_text(step)
        trace_chunks.append(text)
        low = text.lower()
        is_edit = any(marker in low for marker in EDIT_MARKERS)
        signal_paths: list[tuple[str, list[str]]] = []
        if is_edit:
            edit_paths = (
                _paths_for_signal(text, "diff_header")
                if DIFF_RE.search(text)
                else _paths_for_signal(text, "material_edit")
            )
            if edit_paths:
                signal_paths.append(("first_edit" if not saw_first_edit else "material_edit", edit_paths))
                saw_first_edit = True
        if DIFF_RE.search(text):
            signal_paths.append(("diff_header", _paths_for_signal(text, "diff_header")))
        stack_paths = _paths_for_signal(text, "stack_trace")
        if stack_paths:
            signal_paths.append(("stack_trace", stack_paths))
        if any(marker in text for marker in TEST_MARKERS):
            signal_paths.append(("failing_test", _paths_for_signal(text, "failing_test")))
        is_open_search = any(marker in low for marker in OPEN_MARKERS + SEARCH_MARKERS)
        if is_open_search:
            for path in _paths_for_signal(text, "single_open_search"):
                open_search_steps.setdefault(path, set()).add(step_idx)
        elif not signal_paths:
            tool_paths = _paths_for_signal(text, "tool_output_path")
            if tool_paths:
                signal_paths.append(("tool_output_path", tool_paths))

        for signal, paths in signal_paths:
            for path in paths:
                events.append(TraceEvent(path, signal, TIER_BY_SIGNAL[signal], step_idx))
        if is_edit:
            break
        if action_steps >= EARLY_ACTION_STEPS:
            break

    for path, steps_seen in open_search_steps.items():
        signal = "repeated_open_search" if len(steps_seen) >= 2 else "single_open_search"
        for step_idx in sorted(steps_seen):
            events.append(TraceEvent(path, signal, TIER_BY_SIGNAL[signal], step_idx))
    events.sort(key=lambda event: (event.step, -event.tier, event.path, event.signal))
    agent_files = summarize_trace_events(events)

    if action_steps < 3 and not any(e.signal == "first_edit" for e in events):
        return TraceParseResult(
            status="too_few_action_steps",
            artifact=str(artifact),
            events=events,
            agent_files=agent_files,
            early_trace_text="\n".join(trace_chunks),
            action_steps=action_steps,
        )
    if not events:
        return TraceParseResult(
            status="no_path_signal",
            artifact=str(artifact),
            events=events,
            agent_files=agent_files,
            early_trace_text="\n".join(trace_chunks),
            action_steps=action_steps,
        )
    return TraceParseResult(
        status="ok",
        artifact=str(artifact),
        events=events,
        agent_files=agent_files,
        early_trace_text="\n".join(trace_chunks),
        action_steps=action_steps,
    )


def summarize_trace_events(events: Iterable[TraceEvent]) -> dict[str, AgentFileEvidence]:
    buckets: dict[str, list[TraceEvent]] = {}
    for event in events:
        buckets.setdefault(event.path, []).append(event)
    out: dict[str, AgentFileEvidence] = {}
    for path, items in buckets.items():
        out[path] = AgentFileEvidence(
            path=path,
            tier=max(item.tier for item in items),
            event_count=len(items),
            first_step=min(item.step for item in items),
            signals=tuple(sorted({item.signal for item in items})),
        )
    return out


def trace_events_to_agent_candidates(agent_files: dict[str, AgentFileEvidence]) -> list[AgentCandidate]:
    evidence_by_tier = {
        4: "material_edit",
        3: "tool_trace",
        2: "generic_open",
        1: "generic_open",
    }
    candidates = [
        AgentCandidate(path=ev.path, score=ev.tier / 4.0, evidence=evidence_by_tier[ev.tier])
        for ev in agent_files.values()
    ]
    candidates.sort(key=lambda c: (c.score, c.path), reverse=True)
    return candidates


def agent_only_files(agent_files: dict[str, AgentFileEvidence], ceiling: int = 7) -> list[str]:
    rows = sorted(
        agent_files.values(),
        key=lambda ev: (-ev.tier, -ev.event_count, ev.first_step, ev.path),
    )
    return [row.path for row in rows[:ceiling]]


def dumb_bounded_union_files(
    ranked_full: list[dict[str, Any]],
    agent_files: dict[str, AgentFileEvidence],
    ceiling: int = 7,
) -> list[str]:
    out: list[str] = []
    for rec in ranked_full[:3]:
        path = normalize_path(str(rec.get("path", "")))
        if path and path not in out:
            out.append(path)
    for path in agent_only_files(agent_files, ceiling=3):
        if path not in out:
            out.append(path)
        if len(out) >= 6:
            break
    return out[:ceiling]


def _graph_neighbors(path: str, graph_db: str | None) -> list[tuple[str, float]]:
    if not graph_db:
        return []
    conn = sqlite3.connect(graph_db)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT n2.file_path, MAX(COALESCE(e.confidence, 0.5))
            FROM edges e
            JOIN nodes n1 ON e.source_id = n1.id
            JOIN nodes n2 ON e.target_id = n2.id
            WHERE n1.file_path = ?
              AND n2.file_path IS NOT NULL
              AND n1.file_path != n2.file_path
            GROUP BY n2.file_path
            """,
            (path,),
        ).fetchall()
    finally:
        conn.close()
    return [(normalize_path(dst), float(conf)) for dst, conf in rows]


def schedule_v82(
    ranked_full: list[dict[str, Any]],
    graph_db: str | None,
    trace_events: Iterable[TraceEvent],
    preferred_max: int = 5,
    hard_ceiling: int = 7,
) -> ScheduleResult:
    gt_by_path = {normalize_path(str(rec.get("path", ""))): rec for rec in ranked_full}
    gt_top3 = [normalize_path(str(rec.get("path", ""))) for rec in ranked_full[:3]]
    gt_top10 = {normalize_path(str(rec.get("path", ""))) for rec in ranked_full[:10]}
    structural_eligible = {
        normalize_path(str(rec.get("path", "")))
        for rec in ranked_full
        if structurally_eligible(rec)
    }
    agent_files = summarize_trace_events(trace_events)
    active: dict[str, ScheduledFile] = {}
    dropped: list[str] = []
    structural_added: list[str] = []
    provisional: list[str] = []

    def admit(path: str, reason: str, ev: AgentFileEvidence | None = None, edge_conf: float = 0.0, anchor: str | None = None) -> None:
        rec = gt_by_path.get(path)
        tier = ev.tier if ev else 0
        event_count = ev.event_count if ev else 0
        first_step = ev.first_step if ev else 999999
        candidate = ScheduledFile(
            path=path,
            tier=tier,
            gt_score=_gt_score(rec),
            event_count=event_count,
            first_step=first_step,
            reason=reason,
            has_gt_support=rec is not None,
            edge_confidence=edge_conf,
            anchor_path=anchor,
        )
        old = active.get(path)
        if old is None or (candidate.tier, candidate.event_count, candidate.gt_score) > (
            old.tier,
            old.event_count,
            old.gt_score,
        ):
            active[path] = candidate

    def ev_sorted(tier: int) -> list[AgentFileEvidence]:
        return sorted(
            [ev for ev in agent_files.values() if ev.tier == tier],
            key=lambda ev: (ev.first_step, ev.path),
        )

    for tier in (4, 3):
        for ev in ev_sorted(tier):
            admit(ev.path, f"tier{tier}_runtime", ev)
    for ev in ev_sorted(2):
        if ev.path in gt_top10 or ev.path in structural_eligible:
            admit(ev.path, "tier2_gt_supported", ev)
    for path in gt_top3:
        if path and path not in active and len(provisional) < 2:
            admit(path, "provisional_gt_anchor")
            provisional.append(path)

    structural_candidates: list[tuple[int, float, float, str, str, AgentFileEvidence]] = []
    for anchor_ev in agent_files.values():
        if anchor_ev.tier < 3:
            continue
        for dst, conf in _graph_neighbors(anchor_ev.path, graph_db):
            if dst in active or dst not in structural_eligible:
                continue
            rec = gt_by_path.get(dst)
            structural_candidates.append((anchor_ev.tier, _gt_score(rec), conf, dst, anchor_ev.path, anchor_ev))
    structural_candidates.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    for _anchor_tier, _score, conf, dst, anchor, anchor_ev in structural_candidates[:2]:
        admit(dst, "trace_gated_structural", None, conf, anchor)
        structural_added.append(dst)

    for ev in ev_sorted(1):
        if len(active) >= hard_ceiling:
            break
        if ev.path in gt_top10:
            admit(ev.path, "tier1_top10", ev)

    while len(active) > preferred_max:
        candidates = list(active.values())
        choice: ScheduledFile | None = None
        for predicate, key in (
            (lambda c: c.tier == 1 and c.path not in gt_top10, lambda c: (c.gt_score, c.event_count, c.path)),
            (lambda c: c.reason == "provisional_gt_anchor", lambda c: (c.gt_score, c.path)),
            (lambda c: c.tier == 2, lambda c: (c.gt_score, c.event_count, c.path)),
            (lambda c: c.reason == "trace_gated_structural", lambda c: (c.gt_score, c.edge_confidence, c.path)),
        ):
            pool = [c for c in candidates if predicate(c)]
            if pool:
                choice = min(pool, key=key)
                break
        if choice is None:
            break
        dropped.append(choice.path)
        active.pop(choice.path, None)

    if len(active) > hard_ceiling:
        tier4 = [c for c in active.values() if c.tier == 4]
        if len(tier4) > hard_ceiling:
            keep = sorted(tier4, key=lambda c: (c.first_step, c.path))[:hard_ceiling]
        else:
            keep = sorted(
                active.values(),
                key=lambda c: (
                    -c.tier,
                    -int(c.has_gt_support),
                    -c.event_count,
                    -c.gt_score,
                    c.first_step,
                    c.path,
                ),
            )[:hard_ceiling]
        keep_paths = {c.path for c in keep}
        dropped.extend(sorted(set(active) - keep_paths))
        active = {c.path: c for c in keep}

    final = sorted(
        active.values(),
        key=lambda c: (-c.tier, -int(c.has_gt_support), -c.event_count, -c.gt_score, c.first_step, c.path),
    )
    return ScheduleResult(
        active_files=[c.path for c in final],
        active_set=final,
        dropped_files=dropped,
        structural_added=[p for p in structural_added if p in active],
        provisional_gt_anchors=[p for p in provisional if p in active],
    )


def mode_hit(paths: Iterable[str], gold_files: Iterable[str]) -> bool:
    return includes_gold(paths, gold_files)
