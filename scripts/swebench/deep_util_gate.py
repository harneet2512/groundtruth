#!/usr/bin/env python3
"""Deep utilization gate analyzer for Track 4 5-task smoke.

Reports per-layer engagement (not just registration) for each completed task.
"""
from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path


FILE_EXT_RE = re.compile(r'[A-Za-z0-9_./\-]+\.(?:py|js|ts|go|rs|java|cpp|c|h|hpp|rb|php|swift|kt|jsx|tsx)')


def find_traj(task_dir: Path) -> Path | None:
    for f in task_dir.rglob("*.traj"):
        return f
    return None


def extract_brief_files(brief: str) -> list[str]:
    refs = re.findall(r'(?:at|in)\s+([A-Za-z0-9_./\-]+\.(?:py|js|ts|go|rs|java|cpp|c|h|rb))', brief)
    return list(dict.fromkeys(refs))[:5]


def _stringify(x) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        out = []
        for item in x:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out.append(json.dumps(item))
            else:
                out.append(str(item))
        return " ".join(out)
    if isinstance(x, dict):
        return json.dumps(x)
    return str(x)


def extract_first_n_file_actions(traj: dict, n: int = 3) -> list[str]:
    files: list[str] = []
    for h in traj.get("history", []):
        text = _stringify(h.get("content")) + " " + _stringify(h.get("action")) + " " + _stringify(h.get("thought"))
        for path in FILE_EXT_RE.findall(text):
            if path not in files:
                files.append(path)
                if len(files) >= n:
                    return files
    return files


def first_assistant_text(traj: dict) -> str:
    for h in traj.get("history", []):
        if h.get("role") == "assistant":
            return _stringify(h.get("content")) + " " + _stringify(h.get("thought"))
    return ""


def analyze_task(task_dir: Path) -> dict:
    iid = task_dir.name
    out: dict = {"id": iid}

    brief_path = task_dir / "gt_brief.txt"
    gate_path = task_dir / "gt_pre_finish_gate.json"
    query_path = task_dir / "gt_query_calls.jsonl"
    reindex_path = task_dir / "gt_reindex.jsonl"
    evidence_dir = task_dir / "gt_evidence"

    # --- L1 brief ---
    brief = ""
    if brief_path.exists():
        brief = brief_path.read_text(errors="replace")
    out["L1_brief_bytes"] = len(brief)
    out["L1_brief_files"] = extract_brief_files(brief)
    # Substring markers from the brief that the agent could echo
    brief_markers = re.findall(r'\[(?:VERIFIED|WARNING|INFO)\][^\n]+', brief)[:5]
    out["L1_brief_markers"] = [m.strip()[:60] for m in brief_markers]

    # --- Trajectory ---
    traj_path = find_traj(task_dir)
    traj: dict = {}
    if traj_path and traj_path.exists():
        try:
            traj = json.loads(traj_path.read_text(errors="replace"))
        except Exception as exc:  # noqa: BLE001
            out["traj_parse_err"] = str(exc)

    out["L1_first_3_files"] = extract_first_n_file_actions(traj, 3)
    out["L1_first_3_hit_brief"] = sum(
        1 for f in out["L1_first_3_files"]
        if any(bf in f or f.endswith(bf.split("/")[-1]) for bf in out["L1_brief_files"])
    )

    fa = first_assistant_text(traj)
    out["L1_first_response_bytes"] = len(fa)
    out["L1_brief_substr_hits_in_first_response"] = sum(
        1 for m in brief_markers if m[:30].strip() in fa
    )

    # --- L2 (status only — sub-fired vs noop) ---
    layers_log = (task_dir / "gt_layers.log")
    out["L2_status"] = "unknown"
    if layers_log.exists():
        last = layers_log.read_text(errors="replace").strip().splitlines()[-1] if layers_log.read_text(errors="replace").strip() else ""
        if "L2=fired" in last:
            out["L2_status"] = "fired"
        elif "L2=noop" in last:
            out["L2_status"] = "noop"

    # --- L3 evidence files ---
    out["L3_evidence_files"] = 0
    out["L3_families_in_first"] = []
    out["L3_next_action_substr_hits"] = 0
    if evidence_dir.exists():
        edit_files = sorted(evidence_dir.glob("edit_*.json"))
        out["L3_evidence_files"] = len(edit_files)
        if edit_files:
            try:
                first = json.loads(edit_files[0].read_text(errors="replace"))
                br = first.get("brief", "") or json.dumps(first)[:5000]
                fams = [f for f in ("CHANGE", "CONTRACT", "PATTERN", "STRUCTURAL", "SEMANTIC", "OBLIGATIONS") if f in br]
                out["L3_families_in_first"] = fams
                # Substring engagement: did the agent's NEXT action reference brief content?
                # Crude: snip non-trivial tokens from brief, check next assistant message.
                tokens = re.findall(r'\b[A-Za-z_][A-Za-z0-9_]{4,}\b', br)
                tokens = [t for t in tokens if t not in {"function", "Function", "class", "Class", "method", "import"}]
                # We don't know exactly which message comes "after" without timestamps,
                # so we settle for: any substring hit in any later assistant message.
                hits = 0
                if traj.get("history"):
                    history_text = " ".join((h.get("content") or "") + (h.get("thought") or "") for h in traj["history"])
                    hits = sum(1 for t in tokens[:20] if t in history_text)
                out["L3_next_action_substr_hits"] = hits
            except Exception as exc:  # noqa: BLE001
                out["L3_parse_err"] = str(exc)

    # --- L4 query distribution ---
    # RC-10 (D-006 / E-fix): delegate the line count to the shared
    # canonical helper so this reader cannot disagree with smoke
    # runner / Track 4 close-wrap / full_potential_analyzer.
    out["L4_queries"] = 0
    out["L4_symbols"] = []
    try:
        from gt_layer_counts import count_layer_calls
        counts = count_layer_calls(task_dir)
        # Report the L4 sum (gt_query + gt_search + gt_navigate) so
        # this analyzer reflects the post 11→4 consolidation
        # surface, not just gt_query.
        out["L4_queries"] = int(counts.get("L4_total", 0))
        out["L4_breakdown"] = {
            "gt_query": counts.get("gt_query", 0),
            "gt_search": counts.get("gt_search", 0),
            "gt_navigate": counts.get("gt_navigate", 0),
        }
    except ImportError:  # pragma: no cover — fallback for legacy envs
        if query_path.exists():
            for line in query_path.read_text(errors="replace").splitlines():
                if not line.strip():
                    continue
                out["L4_queries"] += 1
    # Symbols: still scrape from gt_query JSONL — the helper returns
    # counts only.
    if query_path.exists():
        for line in query_path.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                q = json.loads(line)
                if "symbol" in q:
                    out["L4_symbols"].append(q["symbol"])
            except Exception:  # noqa: BLE001
                pass

    # --- L5 gate ---
    out["L5_verdict"] = "absent"
    out["L5_checks"] = {}
    if gate_path.exists():
        try:
            g = json.loads(gate_path.read_text(errors="replace"))
            out["L5_verdict"] = g.get("result") or g.get("verdict") or "unknown"
            checks = g.get("checks", {}) or {}
            for k, v in checks.items():
                if isinstance(v, dict):
                    out["L5_checks"][k] = {"triggered": v.get("triggered") or v.get("flagged"), "items": len(v.get("items") or []) }
                else:
                    out["L5_checks"][k] = v
        except Exception as exc:  # noqa: BLE001
            out["L5_parse_err"] = str(exc)

    # --- L6 reindex ---
    out["L6_reindex_events"] = 0
    out["L6_durations_ms"] = []
    if reindex_path.exists():
        for line in reindex_path.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            out["L6_reindex_events"] += 1
            try:
                r = json.loads(line)
                if "duration_ms" in r:
                    out["L6_durations_ms"].append(r["duration_ms"])
            except Exception:  # noqa: BLE001
                pass

    # --- Cost / calls ---
    if traj:
        info = traj.get("info") or {}
        ms = info.get("model_stats") or {}
        out["api_calls"] = ms.get("api_calls")
        out["instance_cost"] = ms.get("instance_cost")
        out["exit_status"] = info.get("exit_status")
        sub = info.get("submission") or ""
        out["submission_bytes"] = len(sub)

    return out


def render_report(rows: list[dict]) -> str:
    lines = []
    lines.append("=" * 78)
    lines.append("DEEP UTILIZATION GATE — engagement evidence per layer per task")
    lines.append("=" * 78)
    for r in rows:
        lines.append("")
        lines.append("─" * 78)
        lines.append(f"TASK: {r['id']}")
        lines.append(f"  exit={r.get('exit_status')!s:<10} api_calls={r.get('api_calls')!s:<5} cost=${r.get('instance_cost', 0):.3f} submission_bytes={r.get('submission_bytes')}")
        lines.append("─" * 78)
        # L1
        lines.append(f"L1 brief: bytes={r['L1_brief_bytes']}  files={r['L1_brief_files']}")
        lines.append(f"   first_3_files={r['L1_first_3_files']}  first_3_hit_brief={r['L1_first_3_hit_brief']}/3")
        lines.append(f"   first_response_bytes={r['L1_first_response_bytes']}  brief_substr_hits={r['L1_brief_substr_hits_in_first_response']}/{len(r['L1_brief_markers'])}")
        # L2
        lines.append(f"L2 status: {r['L2_status']}")
        # L3
        lines.append(f"L3 evidence_files={r['L3_evidence_files']}  families_in_first={r['L3_families_in_first']}  next_action_token_hits={r['L3_next_action_substr_hits']}")
        # L4
        lines.append(f"L4 queries={r['L4_queries']}  symbols(uniq)={list(dict.fromkeys(r['L4_symbols']))[:5]}")
        # L5
        l5 = r['L5_checks']
        lines.append(f"L5 verdict={r['L5_verdict']}  checks={list(l5.keys())}")
        for k, v in l5.items():
            lines.append(f"     {k}: {v}")
        # L6
        d = r['L6_durations_ms']
        if d:
            d_sorted = sorted(d)
            p95 = d_sorted[int(0.95 * len(d_sorted))] if len(d_sorted) > 1 else d_sorted[0]
            lines.append(f"L6 reindex={r['L6_reindex_events']}  p95_ms={p95}  median_ms={d_sorted[len(d_sorted)//2]}")
        else:
            lines.append(f"L6 reindex={r['L6_reindex_events']} (no durations — no events)")

    lines.append("")
    lines.append("=" * 78)
    lines.append("PASS CRITERIA SUMMARY")
    lines.append("=" * 78)
    n = len(rows)
    n_l1_substantive = sum(1 for r in rows if r['L1_brief_bytes'] >= 200 and r['L1_first_3_hit_brief'] >= 1)
    n_l3 = sum(1 for r in rows if r['L3_evidence_files'] > 0)
    n_l5 = sum(1 for r in rows if r['L5_verdict'] not in ('absent', 'unknown'))
    n_l6 = sum(1 for r in rows if r['L6_reindex_events'] > 0)
    lines.append(f"L1 substantive (≥200B brief AND first-3 hit ≥1): {n_l1_substantive}/{n}")
    lines.append(f"L3 evidence files written: {n_l3}/{n}")
    lines.append(f"L5 gate verdict rendered: {n_l5}/{n}")
    lines.append(f"L6 reindex events fired: {n_l6}/{n}  ← KNOWN BUG: L3 edit-counter not catching edit_anthropic invocations")
    return "\n".join(lines)


if __name__ == "__main__":
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/home/ubuntu/swebench_runs")
    if not run_dir.is_dir():
        print(f"FATAL: {run_dir} not a dir", file=sys.stderr)
        sys.exit(1)
    rows = []
    for d in sorted(run_dir.iterdir()):
        if not d.is_dir():
            continue
        if not (d / "gt_brief.txt").exists():
            continue
        rows.append(analyze_task(d))
    if not rows:
        print("FATAL: no tasks found in", run_dir)
        sys.exit(1)
    print(render_report(rows))
