#!/usr/bin/env python3
"""Analyze GT telemetry from a canonical run.

Reads per-instance telemetry JSONL and budget files to produce
aggregated stats for go/no-go decisions and submission.

Usage:
    python canonical/scripts/analyze_telemetry.py \\
        --input-dir results/canonical_gt_v1 \\
        [--verbose]
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("telemetry")


def analyze_instance(
    telem_path: Path,
    budget_path: Path,
) -> dict:
    """Analyze a single instance's GT telemetry."""
    stats: dict = {
        "instance_id": telem_path.stem.replace(".telemetry", ""),
        "events_total": 0,
        "briefing_emitted": False,
        "briefing_tier": None,
        "post_edit_count": 0,
        "budget_final": {},
        "hash_skips": 0,
        "gt_calls": {"orient": 0, "lookup": 0, "impact": 0, "check": 0},
        "evidence_families_seen": set(),
    }

    # Parse telemetry JSONL
    if telem_path.exists():
        for line in telem_path.read_text().strip().split("\n"):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            stats["events_total"] += 1
            ev_type = event.get("event", "")

            if ev_type == "pre_edit_briefing":
                if event.get("status") == "emitted":
                    stats["briefing_emitted"] = True
                    stats["briefing_tier"] = event.get("tier")

            elif ev_type == "post_edit":
                stats["post_edit_count"] += 1
                for fam in event.get("families", []):
                    stats["evidence_families_seen"].add(fam)

            elif ev_type == "budget_snapshot":
                for tool in ("orient", "lookup", "impact", "check"):
                    count = event.get(tool, 0)
                    if count > stats["gt_calls"][tool]:
                        stats["gt_calls"][tool] = count

    # Parse budget file
    if budget_path.exists():
        try:
            budget = json.loads(budget_path.read_text())
            stats["budget_final"] = budget
            for tool in ("orient", "lookup", "impact", "check"):
                count = budget.get(tool, 0)
                if count > stats["gt_calls"][tool]:
                    stats["gt_calls"][tool] = count
        except Exception:
            pass

    # Convert set to list for JSON serialization
    stats["evidence_families_seen"] = sorted(stats["evidence_families_seen"])
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze GT telemetry")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    gt_logs = input_dir / "gt_logs"

    if not gt_logs.exists():
        logger.error("No gt_logs directory found in %s", input_dir)
        return

    telem_files = sorted(gt_logs.glob("*.telemetry.jsonl"))
    budget_files = {p.stem.replace(".budget", ""): p for p in gt_logs.glob("*.budget.json")}

    logger.info("Found %d telemetry files, %d budget files", len(telem_files), len(budget_files))

    all_stats = []
    for telem_path in telem_files:
        instance_id = telem_path.stem.replace(".telemetry", "")
        budget_path = budget_files.get(instance_id, Path("/dev/null"))
        stats = analyze_instance(telem_path, budget_path)
        all_stats.append(stats)

    if not all_stats:
        logger.warning("No telemetry to analyze")
        return

    # Aggregation
    n = len(all_stats)
    briefing_count = sum(1 for s in all_stats if s["briefing_emitted"])
    total_orient = sum(s["gt_calls"]["orient"] for s in all_stats)
    total_lookup = sum(s["gt_calls"]["lookup"] for s in all_stats)
    total_impact = sum(s["gt_calls"]["impact"] for s in all_stats)
    total_check = sum(s["gt_calls"]["check"] for s in all_stats)

    # Budget exhaustion
    budget_exceeded = {
        "orient": sum(1 for s in all_stats if s["gt_calls"]["orient"] > 1),
        "lookup": sum(1 for s in all_stats if s["gt_calls"]["lookup"] > 2),
        "impact": sum(1 for s in all_stats if s["gt_calls"]["impact"] > 2),
        "check": sum(1 for s in all_stats if s["gt_calls"]["check"] > 3),
    }

    # All families seen
    all_families: set[str] = set()
    for s in all_stats:
        all_families.update(s["evidence_families_seen"])

    report = {
        "instances_analyzed": n,
        "briefing": {
            "emitted_count": briefing_count,
            "emitted_rate": round(briefing_count / n, 3),
            "tiers": {},
        },
        "gt_calls": {
            "orient_total": total_orient,
            "orient_avg": round(total_orient / n, 2),
            "lookup_total": total_lookup,
            "lookup_avg": round(total_lookup / n, 2),
            "impact_total": total_impact,
            "impact_avg": round(total_impact / n, 2),
            "check_total": total_check,
            "check_avg": round(total_check / n, 2),
        },
        "budget_exceeded": budget_exceeded,
        "evidence_families_seen": sorted(all_families),
        "anti_spam": {
            "orient_exactly_1": sum(1 for s in all_stats if s["gt_calls"]["orient"] == 1),
            "check_median": sorted(s["gt_calls"]["check"] for s in all_stats)[n // 2],
            "repeated_check_above_3": budget_exceeded["check"],
        },
    }

    # Tier distribution
    for s in all_stats:
        tier = s.get("briefing_tier") or "none"
        report["briefing"]["tiers"][tier] = report["briefing"]["tiers"].get(tier, 0) + 1

    # Go/no-go assessment
    go_nogo = []
    if report["gt_calls"]["orient_avg"] > 1.1:
        go_nogo.append("WARN: orient avg > 1.0 (should be exactly 1)")
    if report["anti_spam"]["check_median"] > 2:
        go_nogo.append("WARN: check median > 2 (possible spam)")
    if any(v > 0 for v in budget_exceeded.values()):
        go_nogo.append(f"WARN: budget exceeded in {budget_exceeded}")
    if report["briefing"]["emitted_rate"] < 0.5:
        go_nogo.append("WARN: briefing emitted < 50% of tasks")

    report["go_nogo"] = go_nogo if go_nogo else ["PASS: all metrics within bounds"]

    # Write report
    report_path = input_dir / "telemetry_report.json"
    report_path.write_text(json.dumps(report, indent=2))
    logger.info("Report written to %s", report_path)

    # Print summary
    print("\n" + "=" * 60)
    print("GT TELEMETRY REPORT")
    print("=" * 60)
    print(f"  Instances analyzed:  {n}")
    print(f"  Briefing rate:       {report['briefing']['emitted_rate']:.1%}")
    print(f"  GT orient avg:       {report['gt_calls']['orient_avg']:.2f}")
    print(f"  GT lookup avg:       {report['gt_calls']['lookup_avg']:.2f}")
    print(f"  GT impact avg:       {report['gt_calls']['impact_avg']:.2f}")
    print(f"  GT check avg:        {report['gt_calls']['check_avg']:.2f}")
    print(f"  Check median:        {report['anti_spam']['check_median']}")
    print(f"  Evidence families:   {', '.join(report['evidence_families_seen'])}")
    print()
    for item in report["go_nogo"]:
        print(f"  {item}")
    print("=" * 60)

    if args.verbose:
        for s in all_stats:
            print(f"\n  {s['instance_id']}:")
            print(f"    briefing={s['briefing_emitted']} tier={s['briefing_tier']}")
            print(f"    calls={s['gt_calls']} post_edits={s['post_edit_count']}")


if __name__ == "__main__":
    main()
