#!/usr/bin/env python3
"""Track D — Deep utilization analysis (BLOCKING gate after 1-task smoke).

Per-layer engagement matrix that proves the agent ACTUALLY USES each GT layer,
not just that it was registered. Per the standing rule:
  "wiring = agent uses the layer, not just registered"

Inputs:
  --log-dir <path>       Per-task log dir (the per-instance subdir under the
                         smoke runner's --output-dir).
                         Required files (best-effort — missing files are
                         flagged but do not crash):
                           gt_brief.txt
                           trajectory.json
                           gt_evidence/edit_NNN.json
                           gt_query_calls.jsonl
                           gt_pre_finish_gate.json
                           gt_reindex.jsonl
                           gt_layers.log
  --instance-id <id>     Optional override; defaults to log_dir.name.
  --output <path>        Markdown report path (default:
                         <log_dir>/deep_utilization_<instance_id>.md).

Engagement signals per layer:

  L1: brief content vs agent's first reasoning turn.
      - substring match against <gt-task-brief> payload (floor)
      - identifier-ordering: agent's first 3 file actions vs top-5 brief files
      - score: 1 if substring hit OR >=1/3 first-3 actions hit top-5 brief
  L3: per-edit hook content vs next-after-edit agent action
      - substring match against gt_evidence/edit_NNN.json content
  L4: gt_query call output vs next-after-query agent action
      - substring match against query output
  L5: gate verdict + agent's reaction (acknowledged + resubmitted, false-block,
      or soft-escape)
  L6: pre/post graph.db hash difference; presence of duration_ms field;
      short-circuit ratio (need separate hash checkpoint to be useful, so
      we report what's recorded in gt_reindex.jsonl)
  L2 (bonus): if L1=fallback, was v22_brief format actually used?

Verdict (per plan §"Pass criteria for the deep gate"):
  - L1 + L3 + L6: substantive engagement observed (not just count > 0).
  - L4: discoverable; engagement scored if invoked.
  - L5: evaluated; gate behavior correct (no false-block).
  - No layer is silently broken (registered but never engaged).

Exit code: 0 = engaged enough to advance; non-zero = blocking gate fail.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---- helpers ---------------------------------------------------------------

_BRIEF_MARKER = "<gt-task-brief>"


def _read_text(p: Path) -> Optional[str]:
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def _read_json(p: Path) -> Optional[Any]:
    text = _read_text(p)
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


def _read_jsonl(p: Path) -> List[Any]:
    out: List[Any] = []
    text = _read_text(p)
    if text is None:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    return out


# ---- trajectory walker -----------------------------------------------------

# SWE-agent 1.x trajectory.json shape varies across versions but consistently
# contains a list of "history" / "trajectory" / "steps" entries with role +
# content + (optionally) tool_calls. Probe several field names.

def _trajectory_steps(traj: Any) -> List[Dict[str, Any]]:
    if isinstance(traj, list):
        return [s for s in traj if isinstance(s, dict)]
    if isinstance(traj, dict):
        for k in ("trajectory", "history", "steps", "messages", "turns"):
            v = traj.get(k)
            if isinstance(v, list):
                return [s for s in v if isinstance(s, dict)]
    return []


def _step_text(step: Dict[str, Any]) -> str:
    parts: List[str] = []
    content = step.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                parts.append(c.get("text", "") or "")
            elif isinstance(c, str):
                parts.append(c)
    parts.append(step.get("thought", "") or "")
    parts.append(step.get("response", "") or "")
    parts.append(step.get("action", "") or "")
    parts.append(step.get("observation", "") or "")
    # tool_calls
    for tc in step.get("tool_calls") or []:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            parts.append(json.dumps(fn))
    return "\n".join(p for p in parts if p)


def _step_role(step: Dict[str, Any]) -> str:
    return str(step.get("role") or step.get("agent_role") or "").lower()


def _step_file_action(step: Dict[str, Any]) -> Optional[str]:
    """If the step is a file-touching tool call, return the file path."""
    # Native tool_calls
    for tc in step.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:  # noqa: BLE001
            args = {}
        if not isinstance(args, dict):
            continue
        if name in (
            "edit", "edit_anthropic", "gt_edit", "str_replace_editor",
            "create", "str_replace", "view", "open", "goto",
        ):
            for k in ("path", "file_path", "filepath", "file"):
                if k in args and args[k]:
                    return str(args[k])
    # Action text shaped like `bash:: edit foo.py` etc.
    action = step.get("action") or ""
    if isinstance(action, str):
        m = re.search(r"(?:open|edit|create|view|str_replace)\s+([^\s]+\.[a-zA-Z0-9_]+)",
                      action)
        if m:
            return m.group(1)
    return None


# ---- L1 engagement ---------------------------------------------------------

@dataclass
class L1Result:
    brief_present: bool
    brief_kind: str           # "primary"|"fallback"|"empty"|"unknown"
    brief_top_files: List[str]
    brief_first_n_chars: str
    first_reasoning_turn_text: str
    substring_hit: bool
    matched_substring: str
    first3_actions: List[str]
    focus_first3_hits: int    # count in [0..3]
    engagement_score: int     # 0 or 1


_TOP_FILE_RE = re.compile(
    r"(?:^|\n)\s*(?:[-*]|\d+[.)])\s*(?:`)?([\w./\-_+]+\.\w+)(?:`)?"
)


def _extract_brief_top_files(brief_text: str, k: int = 5) -> List[str]:
    """Heuristic: pull the first K file-shaped tokens out of the brief.

    The brief format (`generate_enhanced_briefing` and `v22_brief`) lists
    candidate files near the top — usually as a markdown list or in a
    `Files:` block. We grab the first K unique paths.
    """
    if not brief_text:
        return []
    seen: List[str] = []
    for m in _TOP_FILE_RE.finditer(brief_text):
        path = m.group(1).strip()
        # Filter obviously-bogus tokens (just an extension, no dir/name body)
        if "/" not in path and "." not in path[:-1]:
            continue
        if path not in seen:
            seen.append(path)
        if len(seen) >= k:
            break
    return seen


def analyze_l1(log_dir: Path, traj: Any) -> L1Result:
    brief_path = log_dir / "gt_brief.txt"
    text = _read_text(brief_path) or ""
    brief_present = bool(text.strip())

    kind = "unknown"
    if not brief_present:
        kind = "empty"
    else:
        kind_file = log_dir / "gt_brief_kind"
        kind_text = _read_text(kind_file)
        if kind_text:
            kt = kind_text.strip().lower()
            if kt in ("v22", "fallback"):
                kind = "fallback"
            elif kt in ("enhanced", "primary"):
                kind = "primary"
        elif "<gt-v22-brief>" in text:
            kind = "fallback"
        elif _BRIEF_MARKER in text:
            kind = "primary"

    top_files = _extract_brief_top_files(text, k=5)

    # First reasoning turn from agent (role=assistant, role=agent, etc.)
    steps = _trajectory_steps(traj)
    first_assistant_text = ""
    first3_actions: List[str] = []
    for s in steps:
        if _step_role(s) in ("assistant", "agent", "model"):
            txt = _step_text(s)
            if txt and not first_assistant_text:
                first_assistant_text = txt
        # collect first 3 file-touching actions (any role; agent's tool calls
        # may surface as role=tool depending on serialization)
        if len(first3_actions) < 3:
            fa = _step_file_action(s)
            if fa:
                first3_actions.append(fa)

    # Substring engagement: look for any non-trivial brief substring (>=20
    # chars) in agent's first reasoning turn. Token-overlap is too noisy;
    # substring guarantees the agent literally referenced the brief content.
    substring_hit = False
    matched = ""
    if brief_present and first_assistant_text:
        candidates: List[str] = []
        # Split brief into lines, keep ones with >=20 chars and non-trivial.
        for line in text.splitlines():
            ln = line.strip()
            if len(ln) >= 20 and not ln.startswith("#") and ln != _BRIEF_MARKER:
                candidates.append(ln)
        for cand in candidates[:50]:
            if cand in first_assistant_text:
                substring_hit = True
                matched = cand[:140]
                break

    # focus_file_edit_rate_first3: how many of agent's first-3 file actions
    # hit top-5 brief files
    norm_brief = {os.path.normpath(p) for p in top_files}
    norm_actions = [os.path.normpath(p) for p in first3_actions]
    hits = sum(1 for a in norm_actions if a in norm_brief
               or any(a.endswith(b) for b in norm_brief))

    score = 1 if (substring_hit or hits >= 1) else 0

    return L1Result(
        brief_present=brief_present,
        brief_kind=kind,
        brief_top_files=top_files,
        brief_first_n_chars=text[:240],
        first_reasoning_turn_text=first_assistant_text[:400],
        substring_hit=substring_hit,
        matched_substring=matched,
        first3_actions=first3_actions,
        focus_first3_hits=hits,
        engagement_score=score,
    )


# ---- L3 engagement ---------------------------------------------------------

@dataclass
class L3Result:
    edit_count: int
    engaged_count: int            # edits whose content surfaced in next action
    engagement_rate: float
    sample_match: str             # first matched substring (for evidence)
    edit_files_present: List[str]


def analyze_l3(log_dir: Path, traj: Any) -> L3Result:
    ev_dir = log_dir / "gt_evidence"
    if not ev_dir.is_dir():
        return L3Result(0, 0, 0.0, "", [])
    edit_files = sorted(p for p in ev_dir.glob("edit_*.json") if p.is_file())
    edits: List[Tuple[Path, str]] = []
    for ef in edit_files:
        text = _read_text(ef) or ""
        # Normalize: pull out hook brief text bodies if structured JSON
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                # Common keys: brief, content, families, body, output
                body_parts = []
                for k in ("brief", "content", "body", "output", "stdout"):
                    v = data.get(k)
                    if isinstance(v, str) and v.strip():
                        body_parts.append(v)
                if isinstance(data.get("families"), dict):
                    for fk, fv in data["families"].items():
                        if isinstance(fv, str):
                            body_parts.append(fv)
                if body_parts:
                    text = "\n".join(body_parts)
        except Exception:  # noqa: BLE001
            pass
        edits.append((ef, text))

    steps = _trajectory_steps(traj)

    engaged = 0
    sample = ""
    for ef, body in edits:
        # Find the trajectory step whose timestamp / index follows this edit.
        # Without timestamps, approximate: an edit whose body content appears
        # ANYWHERE downstream in agent turns is considered engaged. This is a
        # floor; the L3 contract is "agent's NEXT-AFTER-EDIT action references
        # hook content" — without per-edit timestamps we widen to "any later
        # step references it."
        candidates: List[str] = []
        for line in (body or "").splitlines():
            ln = line.strip()
            if len(ln) >= 20:
                candidates.append(ln)
        hit = False
        for s in steps:
            if _step_role(s) not in ("assistant", "agent", "model"):
                continue
            stext = _step_text(s)
            if not stext:
                continue
            for cand in candidates[:40]:
                if cand in stext:
                    hit = True
                    if not sample:
                        sample = cand[:140]
                    break
            if hit:
                break
        if hit:
            engaged += 1

    n = len(edits)
    rate = (engaged / n) if n else 0.0
    return L3Result(
        edit_count=n,
        engaged_count=engaged,
        engagement_rate=rate,
        sample_match=sample,
        edit_files_present=[p.name for p in edit_files],
    )


# ---- L4 engagement ---------------------------------------------------------

@dataclass
class L4Result:
    query_count: int
    engaged_count: int
    discoverable: bool   # tool present in trajectory tool list / called once
    sample_match: str


def analyze_l4(log_dir: Path, traj: Any) -> L4Result:
    qcalls = _read_jsonl(log_dir / "gt_query_calls.jsonl")
    n = len(qcalls)
    steps = _trajectory_steps(traj)

    engaged = 0
    sample = ""
    for q in qcalls:
        if not isinstance(q, dict):
            continue
        body = ""
        for k in ("output", "result", "stdout", "response"):
            v = q.get(k)
            if isinstance(v, str) and v.strip():
                body = v
                break
        candidates = [ln.strip() for ln in body.splitlines()
                      if len(ln.strip()) >= 20]
        hit = False
        for s in steps:
            if _step_role(s) not in ("assistant", "agent", "model"):
                continue
            stext = _step_text(s)
            for cand in candidates[:40]:
                if cand in stext:
                    hit = True
                    if not sample:
                        sample = cand[:140]
                    break
            if hit:
                break
        if hit:
            engaged += 1

    # Discoverability: did the agent ever call gt_query, or was the tool name
    # surfaced in any system/tool-list message?
    discoverable = n > 0
    if not discoverable:
        for s in steps:
            if "gt_query" in _step_text(s):
                discoverable = True
                break

    return L4Result(
        query_count=n,
        engaged_count=engaged,
        discoverable=discoverable,
        sample_match=sample,
    )


# ---- L5 engagement ---------------------------------------------------------

@dataclass
class L5Result:
    evaluated: bool
    verdict: str
    per_check: Dict[str, Any]
    agent_acknowledged: bool
    agent_resubmitted: bool
    false_block_suspected: bool
    soft_escape_used: bool


def analyze_l5(log_dir: Path, traj: Any) -> L5Result:
    gate = _read_json(log_dir / "gt_pre_finish_gate.json")
    if not isinstance(gate, dict):
        return L5Result(
            evaluated=False, verdict="",
            per_check={}, agent_acknowledged=False,
            agent_resubmitted=False, false_block_suspected=False,
            soft_escape_used=False,
        )
    verdict = str(gate.get("verdict") or "").lower()
    per_check = gate.get("checks") or gate.get("per_check") or {}
    if not isinstance(per_check, dict):
        per_check = {"_raw": per_check}

    # Acknowledged + resubmitted detection: look for two `submit` invocations
    # in the trajectory after a warn verdict.
    steps = _trajectory_steps(traj)
    submit_indices: List[int] = []
    for i, s in enumerate(steps):
        text = _step_text(s)
        if "submit" in text.lower():
            for tc in s.get("tool_calls") or []:
                fn = (tc.get("function") if isinstance(tc, dict) else None) or {}
                if (fn.get("name") or "").lower() == "submit":
                    submit_indices.append(i)
                    break

    ack = False
    resub = False
    if verdict == "warn":
        ack = len(submit_indices) >= 2 or any(
            "acknowledg" in _step_text(s).lower() for s in steps
        )
        resub = len(submit_indices) >= 2

    soft_escape = bool(gate.get("soft_escape") or gate.get("escape_used"))
    # False-block: gate said fail but trajectory shows agent never had a
    # chance to resubmit (no warn-then-submit cycle).
    false_block = verdict == "fail" and not resub

    return L5Result(
        evaluated=verdict in ("pass", "warn", "fail"),
        verdict=verdict,
        per_check=per_check,
        agent_acknowledged=ack,
        agent_resubmitted=resub,
        false_block_suspected=false_block,
        soft_escape_used=soft_escape,
    )


# ---- L6 engagement ---------------------------------------------------------

@dataclass
class L6Result:
    reindex_count: int
    short_circuit_count: int
    real_reparse_count: int
    p95_duration_ms: float
    pre_post_hashes_observed: int
    pre_post_hashes_differed: int


def analyze_l6(log_dir: Path) -> L6Result:
    rj = _read_jsonl(log_dir / "gt_reindex.jsonl")
    n = len(rj)
    short = 0
    reparsed = 0
    durations: List[float] = []
    pp_seen = 0
    pp_diff = 0
    for r in rj:
        if not isinstance(r, dict):
            continue
        if r.get("short_circuited") or r.get("short_circuit"):
            short += 1
        nr = r.get("nodes_replaced", 0)
        er = r.get("edges_replaced", 0)
        if isinstance(nr, int) and isinstance(er, int) and (nr > 0 or er > 0):
            reparsed += 1
        d = r.get("duration_ms")
        if isinstance(d, (int, float)):
            durations.append(float(d))
        pre = r.get("pre_hash") or r.get("graph_db_pre_hash")
        post = r.get("post_hash") or r.get("graph_db_post_hash")
        if pre and post:
            pp_seen += 1
            if pre != post:
                pp_diff += 1
    durations.sort()
    p95 = durations[int(len(durations) * 0.95) - 1] if durations else 0.0
    if not durations:
        p95 = 0.0
    return L6Result(
        reindex_count=n,
        short_circuit_count=short,
        real_reparse_count=reparsed,
        p95_duration_ms=p95,
        pre_post_hashes_observed=pp_seen,
        pre_post_hashes_differed=pp_diff,
    )


# ---- bonus: tool-call sequence echoes brief identifier ordering -----------

def analyze_bonus_ordering(l1: L1Result) -> Tuple[bool, str]:
    """If agent's first 3 actions match the brief's top-N file ordering
    (in order, not just set-membership), report that as bonus engagement."""
    if not l1.first3_actions or not l1.brief_top_files:
        return False, ""
    norm_brief = [os.path.normpath(p) for p in l1.brief_top_files]
    norm_actions = [os.path.normpath(p) for p in l1.first3_actions]

    def _match(a: str, b: str) -> bool:
        return a == b or a.endswith(b) or b.endswith(a)

    # Find first matched indices in brief order; check monotonic-increasing.
    indices: List[int] = []
    for a in norm_actions:
        for i, b in enumerate(norm_brief):
            if _match(a, b):
                indices.append(i)
                break
    if len(indices) >= 2 and indices == sorted(indices):
        return True, f"agent followed brief order: {indices}"
    return False, ""


# ---- verdict ---------------------------------------------------------------

@dataclass
class Verdict:
    passed: bool
    reasons: List[str]


def compute_verdict(
    l1: L1Result, l3: L3Result, l4: L4Result,
    l5: L5Result, l6: L6Result,
) -> Verdict:
    reasons: List[str] = []

    # L1: substantive engagement
    if not l1.brief_present:
        reasons.append("L1: brief not produced")
    elif l1.engagement_score < 1:
        reasons.append(
            "L1: brief produced but agent did not reference it "
            "(no substring hit AND no first-3 action overlap with top-5 files)"
        )

    # L3: substantive engagement
    if l3.edit_count == 0:
        reasons.append("L3: no edits captured (gt_evidence empty)")
    elif l3.engaged_count == 0:
        reasons.append(
            f"L3: {l3.edit_count} edits produced hook briefs, "
            "but none referenced in any later agent turn"
        )

    # L4: discoverable; engagement scored if invoked
    if not l4.discoverable:
        reasons.append("L4: gt_query never discoverable in trajectory")
    elif l4.query_count > 0 and l4.engaged_count == 0:
        reasons.append(
            f"L4: {l4.query_count} queries returned, "
            "none referenced downstream"
        )

    # L5: evaluated; no false-block
    if not l5.evaluated:
        reasons.append("L5: gate never evaluated (no verdict)")
    if l5.false_block_suspected:
        reasons.append("L5: suspected false-block (fail verdict, no resubmit window)")

    # L6: substantive (reindex_count >= 1) AND p95 < 5000 ms (hard ceiling)
    if l6.reindex_count == 0:
        reasons.append("L6: never fired (gt_reindex.jsonl empty)")
    if l6.p95_duration_ms > 5000:
        reasons.append(
            f"L6: p95 duration {l6.p95_duration_ms:.0f}ms exceeds 5s ceiling"
        )

    return Verdict(passed=not reasons, reasons=reasons)


# ---- markdown rendering ----------------------------------------------------

def render_report(
    instance_id: str,
    log_dir: Path,
    l1: L1Result, l3: L3Result, l4: L4Result,
    l5: L5Result, l6: L6Result,
    bonus_ordering: Tuple[bool, str],
    verdict: Verdict,
) -> str:
    out: List[str] = []
    out.append(f"# Deep utilization — {instance_id}\n")
    out.append(f"Source dir: `{log_dir}`\n")

    out.append("## Layer engagement matrix\n")
    out.append("| Layer | Count / Verdict | Engagement | Notes |")
    out.append("|-------|-----------------|------------|-------|")
    out.append(
        f"| L1 (brief) | kind={l1.brief_kind}, top_files={len(l1.brief_top_files)} "
        f"| score={l1.engagement_score} (substring={l1.substring_hit}, "
        f"first3_hits={l1.focus_first3_hits}/3) "
        f"| matched=`{l1.matched_substring or '-'}` |"
    )
    out.append(
        f"| L2 (fallback) | {('fired' if l1.brief_kind == 'fallback' else 'noop')} "
        f"| {('used' if l1.brief_kind == 'fallback' else 'noop_correct')} "
        f"| - |"
    )
    out.append(
        f"| L3 (post-edit hook) | edits={l3.edit_count} "
        f"| engaged={l3.engaged_count}/{l3.edit_count} "
        f"(rate={l3.engagement_rate:.2f}) "
        f"| sample=`{l3.sample_match or '-'}` |"
    )
    out.append(
        f"| L4 (gt_query) | queries={l4.query_count}, discoverable={l4.discoverable} "
        f"| engaged={l4.engaged_count}/{l4.query_count} "
        f"| sample=`{l4.sample_match or '-'}` |"
    )
    out.append(
        f"| L5 (pre-finish gate) | verdict={l5.verdict or '-'} "
        f"| ack={l5.agent_acknowledged}, resubmit={l5.agent_resubmitted}, "
        f"false_block={l5.false_block_suspected}, soft_escape={l5.soft_escape_used} "
        f"| - |"
    )
    out.append(
        f"| L6 (reindex) | n={l6.reindex_count} "
        f"(short={l6.short_circuit_count}, reparsed={l6.real_reparse_count}) "
        f"| p95={l6.p95_duration_ms:.0f}ms "
        f"| hashes diff={l6.pre_post_hashes_differed}/{l6.pre_post_hashes_observed} |"
    )
    out.append("")

    out.append("## L1 detail\n")
    out.append(f"- brief_first_n_chars (240): `{l1.brief_first_n_chars}`")
    out.append(f"- top_files (top-5): {l1.brief_top_files}")
    out.append(f"- agent first reasoning turn (400): `{l1.first_reasoning_turn_text}`")
    out.append(f"- agent first-3 file actions: {l1.first3_actions}")
    out.append("")

    bonus_hit, bonus_msg = bonus_ordering
    if bonus_hit:
        out.append(f"### Bonus engagement\n- **identifier-ordering match**: {bonus_msg}\n")

    out.append("## L3 detail\n")
    out.append(f"- edit files: {l3.edit_files_present}")
    out.append("")

    out.append("## L5 per-check detail\n")
    out.append("```json")
    out.append(json.dumps(l5.per_check, indent=2, default=str)[:4000])
    out.append("```\n")

    out.append("## Verdict\n")
    if verdict.passed:
        out.append("**PASS** — deep utilization gate satisfied; advance to 5-task smoke.")
    else:
        out.append("**FAIL** — deep utilization gate blocks 5-task advance:")
        for r in verdict.reasons:
            out.append(f"  - {r}")
    out.append("")
    return "\n".join(out)


# ---- main ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deep utilization gate (post 1-task smoke, pre 5-task)"
    )
    parser.add_argument("--log-dir", required=True,
                        help="Per-task log dir (e.g. <output>/<instance_id>/)")
    parser.add_argument("--instance-id", default=None)
    parser.add_argument("--output", default=None,
                        help="Markdown report path (default in log-dir)")
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    if not log_dir.is_dir():
        print(f"FATAL: --log-dir not a directory: {log_dir}", file=sys.stderr)
        return 2

    instance_id = args.instance_id or log_dir.name

    traj = _read_json(log_dir / "trajectory.json") or []

    l1 = analyze_l1(log_dir, traj)
    l3 = analyze_l3(log_dir, traj)
    l4 = analyze_l4(log_dir, traj)
    l5 = analyze_l5(log_dir, traj)
    l6 = analyze_l6(log_dir)
    bonus = analyze_bonus_ordering(l1)
    verdict = compute_verdict(l1, l3, l4, l5, l6)

    report = render_report(instance_id, log_dir, l1, l3, l4, l5, l6,
                           bonus, verdict)

    out_path = Path(args.output) if args.output else \
        (log_dir / f"deep_utilization_{instance_id}.md")
    out_path.write_text(report, encoding="utf-8")

    # Print to stdout so the caller can render inline (per plan).
    print(report)

    return 0 if verdict.passed else 1


if __name__ == "__main__":
    sys.exit(main())
