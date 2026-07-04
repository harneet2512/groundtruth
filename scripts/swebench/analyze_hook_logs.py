#!/usr/bin/env python3
"""Analyze GT hook logs to measure evidence emission effectiveness.

Supports both v4 hook log format (evidence dict + abstention_summary)
and the older startupmode format (mode='check_quiet'/'enrich').

Usage:
    # Smoke-test gate check (v4 format, positional log dir):
    python analyze_hook_logs.py /path/to/gt_logs/ --smoke-gate 3

    # Full A/B comparison (older format):
    python analyze_hook_logs.py --gt-output ~/results/gt \\
                                --baseline-output ~/results/baseline

    # Single-dir analysis with per-task detail:
    python analyze_hook_logs.py --hook-logs /path/to/logs/ --detail

    # JSON output:
    python analyze_hook_logs.py /path/to/gt_logs/ --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_logs_dir(log_dir: str) -> tuple[list[dict], list[str]]:
    """Load per-task JSONL files from a flat directory (new format).

    Files are named <instance_id>.jsonl, one JSON object per line.
    """
    entries: list[dict] = []
    errors:  list[str]  = []
    for p in sorted(Path(log_dir).glob("*.jsonl")):
        try:
            with open(p) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entry["_instance_id"] = p.stem
                        entries.append(entry)
                    except json.JSONDecodeError as exc:
                        errors.append(f"{p.name}: {exc}")
        except OSError as exc:
            errors.append(f"{p.name}: {exc}")
    return entries, errors


def load_logs_tree(log_dir: str) -> list[dict]:
    """Load gt_hook_log.jsonl files from a directory tree (old format)."""
    entries: list[dict] = []
    for root, _, files in os.walk(log_dir):
        for fname in files:
            if fname in ("gt_hook_log.jsonl",) or fname.endswith("_hook_log.jsonl"):
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath) as fh:
                        for line in fh:
                            line = line.strip()
                            if line:
                                try:
                                    entry = json.loads(line)
                                    entry["_source_file"] = fpath
                                    entries.append(entry)
                                except json.JSONDecodeError:
                                    pass
                except OSError:
                    pass
    return entries


def load_results(output_dir: str) -> dict[str, dict]:
    """Load output.jsonl from an eval run. Returns dict: instance_id -> result."""
    results: dict[str, dict] = {}
    output_file = os.path.join(output_dir, "output.jsonl")
    if not os.path.exists(output_file):
        return results
    with open(output_file) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    entry = json.loads(line)
                    iid = entry.get("instance_id", "")
                    if iid:
                        results[iid] = entry
                except json.JSONDecodeError:
                    pass
    return results


# ---------------------------------------------------------------------------
# v4 analysis (new hook format: evidence dict + abstention_summary)
# ---------------------------------------------------------------------------

def _is_v4_entry(entry: dict) -> bool:
    return "evidence" in entry and isinstance(entry["evidence"], dict)


def analyse_v4(entries: list[dict]) -> dict:
    """Analyse v4 post-edit hook log entries."""
    total = len(entries)
    if total == 0:
        return {"total_invocations": 0}

    fired        = sum(1 for e in entries if e.get("output", "").strip())
    view_skipped = sum(1 for e in entries if not e.get("files_changed") and not e.get("output"))
    wall_times   = [e["wall_time_ms"] for e in entries if "wall_time_ms" in e]

    family_raw:     Counter[str] = Counter()
    family_emitted: Counter[str] = Counter()
    family_errors:  Counter[str] = Counter()

    for e in entries:
        for family, sig in e.get("evidence", {}).items():
            if not isinstance(sig, dict):
                continue
            if sig.get("ran"):
                family_raw[family]     += sig.get("items_found", 0)
                family_emitted[family] += sig.get("after_abstention", 0)
            if "error" in sig:
                family_errors[family]  += 1

    total_raw      = sum(e.get("abstention_summary", {}).get("total_raw", 0)     for e in entries)
    total_emitted  = sum(e.get("abstention_summary", {}).get("total_emitted", 0) for e in entries)

    output_lines: list[str] = []
    for e in entries:
        out = e.get("output", "")
        if out:
            output_lines.extend(out.strip().splitlines())

    msg_counter:    Counter[str] = Counter()
    family_tag_ctr: Counter[str] = Counter()
    for line in output_lines:
        msg = line.removeprefix("GT: ").strip()
        msg_counter[msg] += 1
        if msg.endswith("]") and "[" in msg:
            tag = msg.rsplit("[", 1)[1].rstrip("]")
            family_tag_ctr[tag] += 1

    tasks: dict[str, dict] = defaultdict(lambda: {"invocations": 0, "fired": 0})
    for e in entries:
        tid = e.get("_instance_id", "unknown")
        tasks[tid]["invocations"] += 1
        if e.get("output", "").strip():
            tasks[tid]["fired"] += 1

    tasks_with_fire = sum(1 for t in tasks.values() if t["fired"] > 0)

    def _p(vals: list, pct: float) -> int:
        if not vals:
            return 0
        return sorted(vals)[int(len(vals) * pct)]

    return {
        "format":              "v4",
        "total_invocations":   total,
        "fired":               fired,
        "emission_rate":       fired / total if total else 0,
        "view_skipped":        view_skipped,
        "tasks_total":         len(tasks),
        "tasks_with_fire":     tasks_with_fire,
        "wall_time_ms": {
            "min":  min(wall_times, default=0),
            "mean": int(sum(wall_times) / len(wall_times)) if wall_times else 0,
            "p95":  _p(wall_times, 0.95),
            "max":  max(wall_times, default=0),
        },
        "abstention": {
            "total_raw":        total_raw,
            "total_emitted":    total_emitted,
            "total_suppressed": total_raw - total_emitted,
            "pass_rate":        total_emitted / total_raw if total_raw else 0,
        },
        "family_raw":      dict(family_raw),
        "family_emitted":  dict(family_emitted),
        "family_errors":   dict(family_errors),
        "top_messages":    msg_counter.most_common(10),
        "family_tag_dist": dict(family_tag_ctr),
        "per_task":        dict(tasks),
    }


def print_v4_report(stats: dict, detail: bool = False) -> None:
    ti   = stats["total_invocations"]
    fire = stats["fired"]
    rate = stats["emission_rate"]

    print("=" * 62)
    print("  GT Hook Effectiveness Report  (v4 format)")
    print("=" * 62)
    print(f"  Hook invocations : {ti}")
    print(f"  Fired (non-empty): {fire}  ({rate*100:.1f}%)")
    print(f"  View skipped     : {stats['view_skipped']}")
    print(f"  Tasks total      : {stats['tasks_total']}")
    print(f"  Tasks with fire  : {stats['tasks_with_fire']}")
    print()

    wt = stats.get("wall_time_ms", {})
    print(f"  Wall time (ms)   :  min={wt.get('min',0)}  mean={wt.get('mean',0)}"
          f"  p95={wt.get('p95',0)}  max={wt.get('max',0)}")
    print()

    ab = stats.get("abstention", {})
    print(f"  Abstention       : {ab.get('total_raw',0)} raw "
          f"-> {ab.get('total_emitted',0)} emitted "
          f"({ab.get('pass_rate',0)*100:.0f}% pass, "
          f"{ab.get('total_suppressed',0)} suppressed)")
    print()

    all_fams = sorted(set(
        list(stats.get("family_raw", {}).keys()) +
        list(stats.get("family_emitted", {}).keys()) +
        list(stats.get("family_errors", {}).keys())
    ))
    if all_fams:
        print("  Evidence families       raw   emitted  errors")
        print("  " + "-" * 44)
        for f in all_fams:
            raw  = stats["family_raw"].get(f, 0)
            emit = stats["family_emitted"].get(f, 0)
            errs = stats["family_errors"].get(f, 0)
            err_str = f"  ERR {errs}" if errs else ""
            print(f"  {f:<14} {raw:>6}  {emit:>8}{err_str}")
        print()

    ftd = stats.get("family_tag_dist", {})
    if ftd:
        print("  Family tag distribution in emitted output:")
        for tag, cnt in sorted(ftd.items(), key=lambda x: -x[1]):
            print(f"    [{tag}]  {cnt}")
        print()

    top = stats.get("top_messages", [])
    if top:
        print("  Top evidence messages:")
        for i, (msg, cnt) in enumerate(top, 1):
            print(f"    {i:2}. ({cnt:>3}×)  {msg[:88]}")
        print()

    if detail:
        print("  Per-task breakdown:")
        for tid, td in sorted(stats["per_task"].items()):
            sym = "+" if td["fired"] > 0 else "."
            print(f"    {sym} {tid:<50}  {td['fired']}/{td['invocations']} fired")
        print()


def smoke_gate(stats: dict, min_tasks: int) -> bool:
    tasks_fired = stats.get("tasks_with_fire", 0)
    crashes     = sum(stats.get("family_errors", {}).values())
    print(f"  Smoke gate: tasks_with_fire={tasks_fired} (need >={min_tasks}), crashes={crashes}")
    passed = tasks_fired >= min_tasks and crashes == 0
    print(f"  Verdict: {'PASS' if passed else 'FAIL'}")
    print()
    return passed


# ---------------------------------------------------------------------------
# v6 analysis (understand + verify)
# ---------------------------------------------------------------------------

def _is_understand_entry(entry: dict) -> bool:
    return entry.get("endpoint") == "understand"


def analyse_v6(entries: list[dict]) -> dict:
    """Analyse v6/v7 entries: split into understand and verify, analyse each."""
    understand = [e for e in entries if _is_understand_entry(e)]
    verify = [e for e in entries if not _is_understand_entry(e) and _is_v4_entry(e)]

    verify_stats = analyse_v4(verify) if verify else {"total_invocations": 0}

    # Understand-specific stats
    tasks_understand: dict[str, dict] = defaultdict(lambda: {"invocations": 0, "output": False})
    for e in understand:
        tid = e.get("_instance_id", "unknown")
        tasks_understand[tid]["invocations"] += 1
        if e.get("output", "").strip():
            tasks_understand[tid]["output"] = True

    total_fingerprinted = sum(
        e.get("fingerprints_extracted", {}).get("fingerprinted", 0) for e in understand
    )
    total_rules_emitted = sum(
        e.get("rules_mined", {}).get("emitted", 0) for e in understand
    )
    total_rules_suppressed = sum(
        e.get("rules_mined", {}).get("suppressed", 0) for e in understand
    )
    shape_computed = sum(
        1 for e in understand if e.get("system_shape", {}).get("computed")
    )
    understand_wall = [e.get("wall_time_ms", 0) for e in understand if "wall_time_ms" in e]
    errors = [e for e in understand if e.get("error")]

    tasks_with_output = sum(1 for t in tasks_understand.values() if t["output"])

    # v7 cross-file intelligence metrics
    is_v7 = any(e.get("what_agent_cannot_get_alone") for e in understand)
    v7_metrics: dict = {}
    if is_v7:
        tasks_crossfile: dict[str, dict] = defaultdict(lambda: {
            "cross_file_callers": False, "test_file_discovery": False, "mined_norms": False
        })
        total_cross_file_symbols = 0
        total_test_files_found = 0
        total_fingerprints_surfaced = 0
        total_norms_surfaced = 0
        index_build_times: list[int] = []
        index_load_times: list[int] = []

        for e in understand:
            tid = e.get("_instance_id", "unknown")
            wacga = e.get("what_agent_cannot_get_alone", {})
            if wacga.get("cross_file_callers"):
                tasks_crossfile[tid]["cross_file_callers"] = True
            if wacga.get("test_file_discovery"):
                tasks_crossfile[tid]["test_file_discovery"] = True
            if wacga.get("mined_norms"):
                tasks_crossfile[tid]["mined_norms"] = True

            cmq = e.get("constraint_map_query", {})
            total_cross_file_symbols += len(cmq.get("cross_file_callers", {}))
            total_test_files_found += len(cmq.get("test_files_found", []))
            total_fingerprints_surfaced += cmq.get("fingerprints_surfaced", 0)
            total_norms_surfaced += cmq.get("norms_surfaced", 0)

            build_ms = e.get("index_build_ms", 0)
            load_ms = e.get("index_load_ms", 0)
            if build_ms > 0:
                index_build_times.append(build_ms)
            if load_ms > 0:
                index_load_times.append(load_ms)

        tasks_with_crossfile = sum(1 for t in tasks_crossfile.values() if t["cross_file_callers"])
        tasks_with_tests = sum(1 for t in tasks_crossfile.values() if t["test_file_discovery"])
        tasks_with_norms = sum(1 for t in tasks_crossfile.values() if t["mined_norms"])

        v7_metrics = {
            "tasks_with_cross_file_callers": tasks_with_crossfile,
            "tasks_with_test_discovery": tasks_with_tests,
            "tasks_with_mined_norms": tasks_with_norms,
            "total_cross_file_symbols": total_cross_file_symbols,
            "total_test_files_found": total_test_files_found,
            "total_fingerprints_surfaced": total_fingerprints_surfaced,
            "total_norms_surfaced": total_norms_surfaced,
            "index_build_ms": {
                "count": len(index_build_times),
                "mean": int(sum(index_build_times) / len(index_build_times)) if index_build_times else 0,
                "max": max(index_build_times, default=0),
            },
            "index_load_ms": {
                "mean": int(sum(index_load_times) / len(index_load_times)) if index_load_times else 0,
                "max": max(index_load_times, default=0),
            },
            "per_task_crossfile": dict(tasks_crossfile),
        }

    return {
        "format": "v7" if is_v7 else "v6",
        "understand": {
            "invocations": len(understand),
            "tasks_total": len(tasks_understand),
            "tasks_with_output": tasks_with_output,
            "fingerprints_extracted": total_fingerprinted,
            "rules_emitted": total_rules_emitted,
            "rules_suppressed": total_rules_suppressed,
            "system_shape_computed": shape_computed,
            "errors": len(errors),
            "wall_time_ms": {
                "min": min(understand_wall, default=0),
                "mean": int(sum(understand_wall) / len(understand_wall)) if understand_wall else 0,
                "max": max(understand_wall, default=0),
            },
            "per_task": dict(tasks_understand),
        },
        "v7_cross_file": v7_metrics,
        "verify": verify_stats,
    }


def print_v6_report(stats: dict, detail: bool = False) -> None:
    u = stats["understand"]
    v = stats["verify"]
    fmt = stats.get("format", "v6")

    print("=" * 62)
    print(f"  GT {fmt} Hook Effectiveness Report")
    print("=" * 62)
    print()
    print("  --- UNDERSTAND (pre-edit) ---")
    print(f"  Invocations      : {u['invocations']}")
    print(f"  Tasks total      : {u['tasks_total']}")
    print(f"  Tasks with output: {u['tasks_with_output']}")
    print(f"  Fingerprints     : {u['fingerprints_extracted']}")
    print(f"  Rules emitted    : {u['rules_emitted']}")
    print(f"  Rules suppressed : {u['rules_suppressed']}")
    print(f"  System shape     : {u['system_shape_computed']} computed")
    print(f"  Errors           : {u['errors']}")
    wt = u.get("wall_time_ms", {})
    print(f"  Wall time (ms)   : min={wt.get('min',0)}  mean={wt.get('mean',0)}  max={wt.get('max',0)}")
    print()

    # v7 cross-file intelligence
    v7 = stats.get("v7_cross_file", {})
    if v7:
        total_tasks = u.get("tasks_total", 0)
        print("  --- V7 CROSS-FILE INTELLIGENCE ---")
        print(f"  Tasks w/ cross-file callers : {v7.get('tasks_with_cross_file_callers', 0)}/{total_tasks}")
        print(f"  Tasks w/ test discovery     : {v7.get('tasks_with_test_discovery', 0)}/{total_tasks}")
        print(f"  Tasks w/ mined norms        : {v7.get('tasks_with_mined_norms', 0)}/{total_tasks}")
        print(f"  Total cross-file symbols    : {v7.get('total_cross_file_symbols', 0)}")
        print(f"  Total test files found      : {v7.get('total_test_files_found', 0)}")
        print(f"  Total fingerprints surfaced : {v7.get('total_fingerprints_surfaced', 0)}")
        print(f"  Total norms surfaced        : {v7.get('total_norms_surfaced', 0)}")
        idx_build = v7.get("index_build_ms", {})
        idx_load = v7.get("index_load_ms", {})
        if idx_build.get("count", 0) > 0:
            print(f"  Index build (ms)            : count={idx_build['count']}  mean={idx_build['mean']}  max={idx_build['max']}")
        if idx_load.get("mean", 0) > 0:
            print(f"  Index load (ms)             : mean={idx_load['mean']}  max={idx_load['max']}")

        # Novel intelligence rate
        novel = (v7.get('tasks_with_cross_file_callers', 0) +
                 v7.get('tasks_with_test_discovery', 0) +
                 v7.get('tasks_with_mined_norms', 0))
        total_possible = total_tasks * 3
        if total_possible > 0:
            print(f"  Novel intelligence rate     : {novel}/{total_possible} ({100*novel/total_possible:.0f}%)")
        print()

        if detail:
            print("  Per-task cross-file breakdown:")
            for tid, td in sorted(v7.get("per_task_crossfile", {}).items()):
                flags = []
                if td.get("cross_file_callers"): flags.append("callers")
                if td.get("test_file_discovery"): flags.append("tests")
                if td.get("mined_norms"): flags.append("norms")
                sym = "+" if flags else "."
                print(f"    {sym} {tid:<50}  {', '.join(flags) if flags else '-'}")
            print()

    if v.get("total_invocations", 0) > 0:
        print("  --- VERIFY (post-edit) ---")
        print(f"  Invocations      : {v['total_invocations']}")
        print(f"  Fired (non-empty): {v.get('fired', 0)}")
        print()

    if detail:
        print("  Per-task understand breakdown:")
        for tid, td in sorted(u.get("per_task", {}).items()):
            sym = "+" if td["output"] else "."
            print(f"    {sym} {tid:<50}  {td['invocations']} calls")
        print()


def smoke_gate_v6(stats: dict, min_tasks: int) -> bool:
    u = stats["understand"]
    tasks_out = u.get("tasks_with_output", 0)
    errors = u.get("errors", 0)
    print(f"  Smoke gate (v6): tasks_with_understand_output={tasks_out} (need >={min_tasks}), errors={errors}")
    passed = tasks_out >= min_tasks and errors == 0
    print(f"  Verdict: {'PASS' if passed else 'FAIL'}")
    print()
    return passed


# ---------------------------------------------------------------------------
# Legacy analysis (old startupmode format: mode='check_quiet'/'enrich')
# ---------------------------------------------------------------------------

def analyse_legacy(entries: list[dict]) -> dict:
    metrics: dict = {
        "total_invocations":         len(entries),
        "enrich_count":              0,
        "enrich_with_output":        0,
        "check_quiet_count":         0,
        "check_with_output":         0,
        "total_obligations_reported": 0,
        "total_suppressed":          0,
        "suppressed_reasons":        defaultdict(int),
        "latencies_ms":              [],
        "enrich_latencies_ms":       [],
        "check_latencies_ms":        [],
    }
    for e in entries:
        mode = e.get("mode", "")
        wt   = e.get("wall_time_ms", 0)
        metrics["latencies_ms"].append(wt)
        if mode == "enrich":
            metrics["enrich_count"] += 1
            metrics["enrich_latencies_ms"].append(wt)
            if e.get("output_lines", 0) > 0:
                metrics["enrich_with_output"] += 1
        elif mode == "check_quiet":
            metrics["check_quiet_count"] += 1
            metrics["check_latencies_ms"].append(wt)
            if e.get("after_abstention", 0) > 0:
                metrics["check_with_output"] += 1
            metrics["total_obligations_reported"] += len(e.get("obligations_reported", []))
            metrics["total_suppressed"] += e.get("suppressed_count", 0)
            for reason in e.get("suppressed_reasons", []):
                metrics["suppressed_reasons"][reason] += 1
    return metrics


def _pct(vals: list, p: int) -> int:
    if not vals:
        return 0
    sv = sorted(vals)
    return sv[min(int(len(sv) * p / 100), len(sv) - 1)]


def format_legacy_report(metrics: dict, gt_results: dict | None = None,
                          baseline_results: dict | None = None) -> str:
    lines = ["=" * 60, "GT HOOK ANALYSIS (legacy format)", "=" * 60]
    lines += [
        f"\nTotal hook invocations:   {metrics['total_invocations']}",
        f"Enrich (read):            {metrics['enrich_count']}  (with output: {metrics['enrich_with_output']})",
        f"Check-quiet (edit):       {metrics['check_quiet_count']}  (with output: {metrics['check_with_output']})",
        f"\nObligations reported:     {metrics['total_obligations_reported']}",
        f"Findings suppressed:      {metrics['total_suppressed']}",
    ]
    if metrics["latencies_ms"]:
        lines += [
            f"\nLatency P50: {_pct(metrics['latencies_ms'], 50)}ms",
            f"Latency P95: {_pct(metrics['latencies_ms'], 95)}ms",
        ]
    if gt_results and baseline_results:
        common = set(gt_results) & set(baseline_results)
        gt_res = sum(1 for tid in common if gt_results[tid].get("resolved"))
        bl_res = sum(1 for tid in common if baseline_results[tid].get("resolved"))
        delta  = gt_res - bl_res
        lines += [
            f"\nA/B: {len(common)} common tasks",
            f"  GT:       {gt_res}/{len(common)} ({gt_res/len(common)*100:.1f}%)" if common else "",
            f"  Baseline: {bl_res}/{len(common)} ({bl_res/len(common)*100:.1f}%)" if common else "",
            f"  Delta:    {delta:+d} ({delta/len(common)*100:+.1f}%)" if common else "",
        ]
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze GT hook logs")
    parser.add_argument("log_dir", nargs="?",
                        help="Directory of per-task *.jsonl log files (v4 format)")
    parser.add_argument("--hook-logs",        help="Directory tree containing gt_hook_log.jsonl files")
    parser.add_argument("--gt-output",        help="GT condition output dir (for A/B)")
    parser.add_argument("--baseline-output",  help="Baseline condition output dir (for A/B)")
    parser.add_argument("--smoke-gate",       type=int, default=0,
                        help="Require at least N tasks to have fired (smoke gate)")
    parser.add_argument("--detail",           action="store_true",
                        help="Show per-task breakdown")
    parser.add_argument("--json",             action="store_true",
                        help="Output raw stats as JSON")
    parser.add_argument("--output", "-o",     help="Write report to file")
    args = parser.parse_args()

    # --- v4 path (positional log_dir with flat *.jsonl files) ---
    if args.log_dir:
        entries, errors = load_logs_dir(args.log_dir)
        if errors:
            for err in errors[:10]:
                print(f"  WARN: {err}", file=sys.stderr)
        if not entries:
            print(f"No log entries found in {args.log_dir}", file=sys.stderr)
            sys.exit(1)

        # Check for v6 entries (has understand endpoint)
        has_understand = any(_is_understand_entry(e) for e in entries)

        if has_understand:
            stats = analyse_v6(entries)
            if args.json:
                print(json.dumps(stats, indent=2, default=str))
                return
            print_v6_report(stats, detail=args.detail)
            if args.smoke_gate > 0:
                passed = smoke_gate_v6(stats, args.smoke_gate)
                sys.exit(0 if passed else 1)
            return

        v4 = [e for e in entries if _is_v4_entry(e)]
        if not v4:
            print("WARNING: no v4-format entries found; falling back to legacy analysis",
                  file=sys.stderr)
            stats_raw = analyse_legacy(entries)
            report = format_legacy_report(stats_raw)
        else:
            stats = analyse_v4(v4)
            if args.json:
                print(json.dumps(stats, indent=2, default=str))
                return
            print_v4_report(stats, detail=args.detail)
            if args.smoke_gate > 0:
                passed = smoke_gate(stats, args.smoke_gate)
                sys.exit(0 if passed else 1)
        return

    # --- Legacy path (--hook-logs / --gt-output / --baseline-output) ---
    entries_legacy: list[dict] = []
    if args.hook_logs:
        entries_legacy = load_logs_tree(args.hook_logs)
    elif args.gt_output:
        entries_legacy = load_logs_tree(args.gt_output)

    if not entries_legacy:
        print("No hook log entries found. Provide a positional log_dir or --hook-logs.",
              file=sys.stderr)
        sys.exit(1)

    metrics = analyse_legacy(entries_legacy)
    gt_res  = load_results(args.gt_output)       if args.gt_output       else None
    bl_res  = load_results(args.baseline_output) if args.baseline_output else None
    report  = format_legacy_report(metrics, gt_res, bl_res)

    if args.output:
        with open(args.output, "w") as fh:
            fh.write(report)
        print(f"Report written to {args.output}")
    else:
        print(report)

    json_metrics = {k: v for k, v in metrics.items() if k != "suppressed_reasons"}
    json_metrics["suppressed_reasons"] = dict(metrics["suppressed_reasons"])
    for key in ("latencies_ms", "enrich_latencies_ms", "check_latencies_ms"):
        vals = json_metrics.pop(key, [])
        if vals:
            json_metrics[f"{key}_p50"] = _pct(vals, 50)
            json_metrics[f"{key}_p95"] = _pct(vals, 95)
            json_metrics[f"{key}_count"] = len(vals)
    json_path = (args.output or "hook_analysis") + ".json"
    if args.output:
        json_path = args.output.rsplit(".", 1)[0] + ".json"
    with open(json_path, "w") as fh:
        json.dump(json_metrics, fh, indent=2)
    print(f"Metrics JSON: {json_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
