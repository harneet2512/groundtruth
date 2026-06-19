#!/usr/bin/env python3
"""Build a Markdown deep-dive: audit context, per-instance layer summary, per-turn GT signals.

Reads OpenHands `output.jsonl` (fields: instance_id, history, instruction, error, test_result, ...).
Does not require `gt_telemetry` (uses the same markers as `oh_gt_full_layer_audit.py` / `gt_utilization_report.py`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Reuse canonical layer definitions
sys.path.insert(0, str(Path(__file__).resolve().parent))
from oh_gt_full_layer_audit import audit_record  # noqa: E402
from gt_utilization_report import (  # noqa: E402
    _infer_layers_from_record,
)

_L4_CMD = re.compile(
    r"\bgt_query\s+\S+|\bgt_search\s+\S+\s+\S+|\bgt_navigate\s+\S+\s+\S+|\bgt_validate\s+\S+",
    re.IGNORECASE,
)
_L5_ADV = re.compile(r"<gt-advisory[^>]{0,120}L5", re.IGNORECASE)
_EVIDENCE = re.compile(
    r"\[(?:GT_CHANGE|GT_CONTRACT|GT_CALLER|GT_SIBLING|GT_PATTERN|GT_STRUCTURAL|GT_SEMANTIC|GT_TEST|GT_IMPORT)\]",
    re.IGNORECASE,
)


def _blob(ev: dict) -> str:
    try:
        return json.dumps(ev, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(ev)


def _snippet(s: str, needle: str, before: int = 120, after: int = 420) -> str:
    i = s.find(needle)
    if i < 0:
        return ""
    frag = s[max(0, i - before) : i + after]
    frag = frag.replace("\\n", "\n")
    frag = re.sub(r"\s+", " ", frag)
    return frag.strip()


def _event_kind(ev: dict) -> str:
    src = str(ev.get("source") or "")
    act = ev.get("action")
    obs = ev.get("observation")
    if src == "user":
        if act == "message":
            return "user_message"
        if act == "recall":
            return "user_recall"
        return f"user:{act}"
    if src == "environment":
        return "environment"
    if src == "agent":
        if obs:
            return f"observation:{obs}"
        if act == "run":
            return "tool:run"
        if act == "read":
            return "tool:read"
        if act == "edit":
            return "tool:edit"
        if act == "think":
            return "tool:think"
        if act is None:
            return "agent"
        return f"tool:{act}"
    return src or "unknown"


def layers_for_event(ev: dict) -> tuple[str, ...]:
    """Which integration layers visibly fire in this single history row (subset)."""

    b = _blob(ev)
    bl = b.lower()
    msg = str(ev.get("message") or "")
    hits: list[str] = []

    if "<gt-task-brief>" in b:
        hits.append("L1_brief_marker")
    if re.search(r"<gt-pretask[^>]{0,500}L2", b):
        hits.append("L2_pretask_marker")

    args = ev.get("args")
    cmd = ""
    if isinstance(args, dict):
        cmd = str(args.get("command") or "")
    if ev.get("action") == "run" and cmd and _L4_CMD.search(cmd):
        hits.append("L4_shell_invocation")
    if "Running command:" in msg:
        tail = msg.split("Running command:", 1)[-1]
        if _L4_CMD.search(tail):
            hits.append("L4_shell_logged")

    if "<gt-reindex" in bl:
        hits.append("L6_reindex")
    if _L5_ADV.search(b):
        hits.append("L5_advisory")

    # Hook stdout is embedded in observations / tool transcripts.
    if "post_edit:" in bl and _EVIDENCE.search(b):
        hits.append("L3_post_edit_evidence")
    if "post_view:" in bl and "[GT_L3B]" in b:
        hits.append("L3b_post_view_structural")
    elif "<gt-evidence trigger=" in bl and "post_view:" in bl.replace("\\", ""):
        hits.append("L3b_post_view_empty_shell")

    # Dedup preserve order
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return tuple(out)


def iter_jsonl(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def render_record(rec: dict) -> str:
    iid = str(rec.get("instance_id") or rec.get("instance") or "?")
    lines: list[str] = []
    lines.append(f"## Instance `{iid}`")
    audit = audit_record(rec)
    inf = _infer_layers_from_record(rec)

    lines.append("")
    lines.append("### Layer snapshot (whole-trajectory)")
    lines.append("")
    lines.append("| Layer | `oh_gt_full_layer_audit` | Inferred utilization (weights) |")
    lines.append("|-------|--------------------------|--------------------------------|")
    for k in ("L1", "L2", "L3", "L3b", "L4", "L5", "L6"):
        aud_key = {
            "L1": "L1_brief",
            "L2": "L2_pretask_telemetry",
            "L3": "L3_post_edit",
            "L3b": "L3b_post_view",
            "L4": "L4_tools",
            "L5": "L5_advisory",
            "L6": "L6_reindex",
        }[k]
        lines.append(
            f"| {k} | {'Y' if audit.get(aud_key) else 'N'} | {float(inf.get(k, 0) or 0):.0f} |"
        )

    hist = rec.get("history")
    lines.append("")
    lines.append(f"### Trajectory shape (`history` length **{len(hist) if isinstance(hist, list) else 0}**)")

    err = rec.get("error")
    tr = rec.get("test_result")
    if err is not None:
        lines.append("")
        lines.append(f"- **error** (truncated): `{str(err)[:240]}` …")
    if tr is not None:
        lines.append(f"- **test_result**: `{json.dumps(tr, default=str)[:400]}` …")

    if not isinstance(hist, list):
        lines.append("")
        lines.append("_No parseable history._")
        lines.append("")
        return "\n".join(lines)

    turn_rows: list[tuple[int, str, str, str]] = []
    for idx, ev in enumerate(hist):
        if not isinstance(ev, dict):
            continue
        kinds = ",".join(layers_for_event(ev))
        if not kinds:
            continue
        b = _blob(ev)
        ex = ""
        if "<gt-task-brief>" in b:
            ex = _snippet(b, "<gt-task-brief>", 0, 700)
        elif "<gt-evidence trigger=" in b:
            ex = _snippet(b, "<gt-evidence", 40, 900)
        elif ev.get("action") == "run":
            cmd = ""
            args = ev.get("args")
            if isinstance(args, dict):
                cmd = str(args.get("command") or "")
            if _L4_CMD.search(cmd):
                ex = cmd[:400]
        if len(ex) > 650:
            ex = ex[:650] + " …"
        turn_rows.append((idx, _event_kind(ev), kinds, ex))

    lines.append("")
    lines.append("### Turns where GroundTruth visibly emitted (markers, hooks, tools, reindex)")
    lines.append("")
    lines.append("| `history` idx | Kind | Layers / markers this turn | Excerpt |")
    lines.append("|---------------|------|-----------------------------|---------|")

    def esc_cell(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", " ").strip()

    for idx, kind, kinds, ex in turn_rows[:80]:
        lines.append(f"| {idx} | {esc_cell(kind)} | {esc_cell(kinds)} | {esc_cell(ex or '')} |")
    if len(turn_rows) > 80:
        lines.append(f"| … | … | _(+{len(turn_rows) - 80} more rows omitted)_ | |")

    # First brief block (deterministic UX story)
    for ev in hist:
        if not isinstance(ev, dict):
            continue
        msg = ev.get("message")
        if ev.get("source") == "user" and isinstance(msg, str) and "<gt-task-brief>" in msg:
            lines.append("")
            lines.append("### Injected startup brief (first user task message, clipped)")
            lines.append("")
            lines.append("```")
            lines.append(msg[:3500])
            if len(msg) > 3500:
                lines.append("…")
            lines.append("```")
            break

    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("jsonl", type=Path, help="Path to output.jsonl")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Write Markdown report here")
    ap.add_argument("--title", default="OH × GroundTruth — comprehensive smoke analysis")
    args = ap.parse_args(argv[1:])

    if not args.jsonl.is_file():
        print(f"Not a file: {args.jsonl}", file=sys.stderr)
        return 2

    records = list(iter_jsonl(args.jsonl))
    records.sort(key=lambda r: str(r.get("instance_id") or ""))

    weights = {"L1": 0.2, "L2": 0.15, "L3": 0.2, "L3b": 0.1, "L4": 0.1, "L5": 0.15, "L6": 0.1}

    blocks: list[str] = []
    blocks.append(f"# {args.title}")
    blocks.append("")
    blocks.append("**Source artifact:** `{}`".format(args.jsonl.as_posix()))
    blocks.append("")
    blocks.append("## 1. Narrative arc (audit → remediation → verification)")
    blocks.append("")
    blocks.append(
        "This report is anchored on the **tiered integration audit** that drove changes to "
        "`oh_gt_full_wrapper.py`, `post_edit.py`, `post_view.py`, smoke gate, utilization, and regression helpers. "
        "The remediation goals were:"
    )
    blocks.append("")
    blocks.append("- **Tier 0/1 bootstrap:** deterministic container workspace probing, gt-index upload with binary fallback, in-container brief generation, `PYTHONPATH`/env prefix correctness, graph build sanity.")
    blocks.append("- **Tier 2/3 observability & depth:** `[GT_*]` hook families, `post_view`/`post_edit` args and status tags, L4 tools installed only after verify, incremental reindex ordering before reads.")
    blocks.append("- **Tier 4 safety:** newline/LF discipline for installer scripts, L5 advisory wiring against validate coverage assumptions (markers may still be absent when `gt_validate` already matched paths).")
    blocks.append("")
    blocks.append(
        "**Verification path:** parallel SWE-bench Lite smoke on VM `gt-t0`, aggregated `output.jsonl` exported for offline analysis "
        "(this file)."
    )
    blocks.append("")
    blocks.append(
        "**Important:** OpenHands transcripts do **not** include a populated `gt_telemetry` JSON field on this dataset run; utilization here uses "
        "the **same inferred marker model** as `gt_utilization_report.py` (fallback path)."
    )
    blocks.append("")
    blocks.append(
        "**L4 nuance (whole-run vs per-turn):** `oh_gt_full_layer_audit` / inference scan the **entire serialized record** "
        "(system prompt, instructions, tool catalogs, and bash output). A line like `gt_validate path` inside the **developer "
        "system message** therefore counts as **L4=Y** even if the agent never ran that command. "
        "Section 3’s per-turn table only flags **L4_shell_\\*** when a concrete `run` / `Running command:` line matches the "
        "`gt_query|gt_search|gt_navigate|gt_validate` pattern—use both views together."
    )

    blocks.append("")
    blocks.append("## 2. Whole-run layer audit + inferred utilization")

    totals = {
        "L1_brief": 0,
        "L2_pretask_telemetry": 0,
        "L3_post_edit": 0,
        "L3b_post_view": 0,
        "L4_tools": 0,
        "L5_advisory": 0,
        "L6_reindex": 0,
    }
    n = 0
    overall_scores: list[float] = []
    rows_md: list[str] = []

    for rec in records:
        ar = audit_record(rec)
        n += 1
        for k, v in ar.items():
            if v:
                totals[k] += 1
        inf = _infer_layers_from_record(rec)
        ov = sum(float(bool(inf.get(k))) * weights[k] for k in weights)
        overall_scores.append(ov)

        rows_md.append(
            "| `{inst}` | {L1} | {L2} | {L3} | {L3b} | {L4} | {L5} | {L6} | {ov:.3f} |".format(
                inst=str(rec.get("instance_id") or ""),
                L1="Y" if ar["L1_brief"] else "N",
                L2="Y" if ar["L2_pretask_telemetry"] else "N",
                L3="Y" if ar["L3_post_edit"] else "N",
                L3b="Y" if ar["L3b_post_view"] else "N",
                L4="Y" if ar["L4_tools"] else "N",
                L5="Y" if ar["L5_advisory"] else "N",
                L6="Y" if ar["L6_reindex"] else "N",
                ov=ov,
            )
        )

    blocks.append("")
    blocks.append(f"Instances **{n}**.")
    blocks.append("")
    blocks.append("| Layer | Hits | Rate |")
    blocks.append("|-------|------|------|")
    for layer_key, nice in (
        ("L1_brief", "L1 brief"),
        ("L2_pretask_telemetry", "L2 pretask"),
        ("L3_post_edit", "L3 post_edit"),
        ("L3b_post_view", "L3b post_view"),
        ("L4_tools", "L4 tools"),
        ("L5_advisory", "L5 advisory"),
        ("L6_reindex", "L6 reindex"),
    ):
        c = totals[layer_key]
        pct = (100.0 * c / n) if n else 0.0
        blocks.append(f"| {nice} | {c}/{n} | {pct:.0f}% |")

    blocks.append("")
    if overall_scores:
        mean = sum(overall_scores) / len(overall_scores)
        blocks.append(
            f"Inferred **overall_utilization** (weighted): mean **{mean:.3f}**, min **{min(overall_scores):.3f}**, max **{max(overall_scores):.3f}**."
        )

    blocks.append("")
    blocks.append("| instance_id | L1 | L2 | L3 | L3b | L4 | L5 | L6 | inferred overall |")
    blocks.append("|-------------|----|----|----|-----|----|----|----|-----------------|")
    blocks.extend(rows_md)

    blocks.append("")
    blocks.append(
        "**How to read L5 here:** audit requires `<gt-advisory … L5`; if agents ran `gt_validate` on touched paths "
        "(or abstained from edits entirely), advisory text may legitimately stay absent → inferred L5 sticks at ~0 unless you "
        "tighten the trigger."
    )

    blocks.append("")
    blocks.append("## 3. Per-instance deep dive (timeline of GT-visible emissions)")
    blocks.append("")
    for rec in records:
        blocks.append(render_record(rec))

    blocks.append("")
    blocks.append(
        "---\n\n_Generated by `scripts/swebench/smoke_deep_trajectory_report.py`. "
        "Layer audit logic: `oh_gt_full_layer_audit.py`. Inference weights: `gt_utilization_report.py`._"
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(blocks), encoding="utf-8")
    print(f"Wrote {args.output} ({args.output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
