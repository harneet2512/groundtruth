#!/usr/bin/env python3
"""Evidence Delivery Smoke Gate for OpenHands.

Proves that GroundTruth evidence was generated, injected, and visible to the
OpenHands agent.  Two subcommands:

    scan   -- read hook logs + output.jsonl trajectories, emit telemetry JSONL
    verify -- read telemetry, check gates, write gate report, exit 0/1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GT_EVIDENCE_MARKERS: list[str] = [
    "<gt-evidence>",
    "GT CODEBASE",
    "CONNECTED CODE",
    "-- structural coupling --",
    "--- OBLIGATIONS ---",
    "NEEDS_FIXES:",
    "INCOMPLETE:",
    "GT: ",
    "[VERIFIED]",
    "[WARNING]",
    "[INFO]",
]

FAMILY_TAGS: list[str] = [
    "CONTRACT",
    "PATTERN",
    "CHANGE",
    "STRUCTURAL",
    "SEMANTIC",
]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TelemetryRecord:
    task_id: str
    scaffold: str = "openhands"
    gt_version: str = "none"
    model: str = ""
    arm: str = "baseline"
    evidence_generated: bool = False
    evidence_injected: bool = False
    evidence_visible_in_trajectory: bool = False
    evidence_tokens: int = 0
    evidence_families: list[str] = field(default_factory=list)
    evidence_block_hash: str = ""
    evidence_count: int = 0
    hook_fire_count: int = 0
    hook_error_count: int = 0
    index_built: bool = False
    index_node_count: int = 0
    config_hash: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    passed: bool
    gates: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _contains_marker(text: str) -> bool:
    """Return True if *text* contains any GT evidence marker (case-sensitive)."""
    if not text:
        return False
    for marker in GT_EVIDENCE_MARKERS:
        if marker in text:
            return True
    return False


def _extract_families(text: str) -> list[str]:
    """Return family tags found in *text* using bracketed format."""
    if not text:
        return []
    return [t for t in FAMILY_TAGS if f"[{t}]" in text or f"[{t}:" in text]


def _sha256_short(data: str, length: int = 12) -> str:
    return hashlib.sha256(data.encode()).hexdigest()[:length]


def _token_estimate(text: str) -> int:
    """Rough token count (~4 chars per token)."""
    return len(text) // 4 if text else 0


# ---------------------------------------------------------------------------
# scan_hook_logs
# ---------------------------------------------------------------------------


def scan_hook_logs(
    gt_log_dir: Path | None,
    arm: str,
    gt_version: str,
    task_ids: list[str],
) -> dict[str, TelemetryRecord]:
    """Scan per-task hook log JSONL files and return partial TelemetryRecords."""
    records: dict[str, TelemetryRecord] = {}

    for tid in task_ids:
        rec = TelemetryRecord(task_id=tid, arm=arm, gt_version=gt_version)

        if gt_log_dir is None:
            records[tid] = rec
            continue

        log_path = gt_log_dir / f"{tid}.jsonl"
        if not log_path.exists():
            rec.errors.append(f"hook log missing: {log_path}")
            records[tid] = rec
            continue

        fire_count = 0
        error_count = 0
        families: set[str] = set()
        has_output = False
        index_built = False
        node_count = 0

        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    error_count += 1
                    continue

                # Count fires (entries with non-empty output)
                output = entry.get("output", "")
                if isinstance(output, dict):
                    output = json.dumps(output)
                if output:
                    fire_count += 1
                    has_output = True
                    families.update(_extract_families(str(output)))

                # Count errors
                if entry.get("error"):
                    error_count += 1

                # Extract mode/family info
                mode = entry.get("mode", "")
                if mode:
                    families.update(_extract_families(str(mode)))

                # Evidence dict families
                evidence = entry.get("evidence", {})
                if isinstance(evidence, dict):
                    for k in evidence:
                        families.update(_extract_families(str(k)))

                # Index info
                if entry.get("index_built"):
                    index_built = True
                if entry.get("node_count", 0) > node_count:
                    node_count = entry.get("node_count", 0)

        rec.evidence_generated = has_output
        rec.evidence_injected = fire_count > 0
        rec.hook_fire_count = fire_count
        rec.hook_error_count = error_count
        rec.evidence_families = sorted(families)
        rec.index_built = index_built
        rec.index_node_count = node_count
        records[tid] = rec

    return records


# ---------------------------------------------------------------------------
# scan_trajectory
# ---------------------------------------------------------------------------


def _search_history_item(item: dict[str, Any]) -> str:
    """Extract all text from a history item that might contain GT evidence."""
    parts: list[str] = []

    # observation field (top-level)
    obs = item.get("observation", "")
    if isinstance(obs, str):
        parts.append(obs)
    elif isinstance(obs, dict):
        parts.append(json.dumps(obs))

    # args.content
    args = item.get("args", {})
    if isinstance(args, dict):
        content = args.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, dict):
            parts.append(json.dumps(content))
        # Also check args.output for CmdOutputObservation etc.
        output = args.get("output", "")
        if isinstance(output, str):
            parts.append(output)

    # top-level content
    content = item.get("content", "")
    if isinstance(content, str):
        parts.append(content)

    # message field
    msg = item.get("message", "")
    if isinstance(msg, str):
        parts.append(msg)

    return "\n".join(parts)


def scan_trajectory(
    output_jsonl: Path,
    arm: str,  # noqa: ARG001
    gt_version: str,  # noqa: ARG001
    task_ids: list[str] | None,
) -> dict[str, dict[str, Any]]:
    """Scan output.jsonl for GT evidence markers in agent observations."""
    results: dict[str, dict[str, Any]] = {}

    if not output_jsonl.exists():
        return results

    with open(output_jsonl, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            instance_id = entry.get("instance_id", "")
            if not instance_id:
                continue
            if task_ids is not None and instance_id not in task_ids:
                continue

            history = entry.get("history", [])
            if not history:
                history = entry.get("messages", [])

            evidence_texts: list[str] = []
            families_seen: set[str] = set()

            for item in history:
                if not isinstance(item, dict):
                    continue
                text = _search_history_item(item)
                if _contains_marker(text):
                    evidence_texts.append(text)
                    families_seen.update(_extract_families(text))

            combined = "\n".join(evidence_texts)
            results[instance_id] = {
                "evidence_visible": len(evidence_texts) > 0,
                "evidence_tokens": _token_estimate(combined),
                "evidence_families": sorted(families_seen),
                "evidence_text_hash": _sha256_short(combined) if combined else "",
                "evidence_count": len(evidence_texts),
            }

    return results


# ---------------------------------------------------------------------------
# merge_telemetry
# ---------------------------------------------------------------------------


def merge_telemetry(
    hook_data: dict[str, TelemetryRecord],
    traj_data: dict[str, dict[str, Any]],
    model: str,
    config_hash: str,
) -> list[TelemetryRecord]:
    """Merge hook log data with trajectory scan data."""
    all_ids = sorted(set(hook_data.keys()) | set(traj_data.keys()))
    records: list[TelemetryRecord] = []

    for tid in all_ids:
        if tid in hook_data:
            rec = hook_data[tid]
        else:
            rec = TelemetryRecord(task_id=tid)

        rec.model = model
        rec.config_hash = config_hash

        if tid in traj_data:
            td = traj_data[tid]
            rec.evidence_visible_in_trajectory = td["evidence_visible"]
            rec.evidence_tokens = max(rec.evidence_tokens, td["evidence_tokens"])
            rec.evidence_block_hash = td.get("evidence_text_hash", "")
            rec.evidence_count = max(rec.evidence_count, td.get("evidence_count", 0))
            # Merge families
            merged_families = set(rec.evidence_families) | set(
                td.get("evidence_families", [])
            )
            rec.evidence_families = sorted(merged_families)
            # If trajectory shows evidence but hook logs were absent (e.g.
            # startupmode wrapper which doesn't extract logs), infer
            # generation and injection from trajectory visibility.
            if td["evidence_visible"] and not rec.evidence_generated:
                rec.evidence_generated = True
                rec.evidence_injected = True

        records.append(rec)

    return records


# ---------------------------------------------------------------------------
# hash_config
# ---------------------------------------------------------------------------


def hash_config(
    metadata_path: Path | None, llm_config_path: Path | None = None
) -> str:
    """Compute a short hash of the run configuration."""
    config: dict[str, Any] = {}

    if metadata_path and metadata_path.exists():
        try:
            with open(metadata_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            for key in ("model", "max_iterations", "scaffold", "dataset"):
                if key in meta:
                    config[key] = meta[key]
        except (json.JSONDecodeError, OSError):
            pass

    if llm_config_path and llm_config_path.exists():
        try:
            with open(llm_config_path, encoding="utf-8") as fh:
                config["llm_config"] = json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass

    if not config:
        return "unknown"

    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return _sha256_short(canonical)


# ---------------------------------------------------------------------------
# check_gates
# ---------------------------------------------------------------------------


def check_gates(records_by_arm: dict[str, list[TelemetryRecord]]) -> GateResult:
    """Check delivery gates across all arms."""
    gates: list[dict[str, Any]] = []
    all_passed = True

    for arm, records in records_by_arm.items():
        n = len(records)
        if n == 0:
            continue

        if arm == "baseline":
            # Baseline contamination check: 0 GT markers allowed
            contaminated = sum(
                1 for r in records if r.evidence_visible_in_trajectory
            )
            passed = contaminated == 0
            if not passed:
                all_passed = False
            gates.append(
                {
                    "name": f"{arm}_no_contamination",
                    "threshold": f"0/{n}",
                    "actual": f"{contaminated}/{n}",
                    "passed": passed,
                    "details": "Baseline must have zero GT evidence markers",
                }
            )
        else:
            # GT arm gates
            generated = sum(1 for r in records if r.evidence_generated)
            passed_gen = generated == n
            if not passed_gen:
                all_passed = False
            gates.append(
                {
                    "name": f"{arm}_evidence_generated",
                    "threshold": f"{n}/{n}",
                    "actual": f"{generated}/{n}",
                    "passed": passed_gen,
                    "details": "All tasks must have evidence generated",
                }
            )

            injected = sum(1 for r in records if r.evidence_injected)
            passed_inj = injected == n
            if not passed_inj:
                all_passed = False
            gates.append(
                {
                    "name": f"{arm}_evidence_injected",
                    "threshold": f"{n}/{n}",
                    "actual": f"{injected}/{n}",
                    "passed": passed_inj,
                    "details": "All tasks must have evidence injected via hook",
                }
            )

            visible = sum(1 for r in records if r.evidence_visible_in_trajectory)
            vis_pct = (visible / n * 100) if n > 0 else 0
            passed_vis = vis_pct >= 80.0
            if not passed_vis:
                all_passed = False
            gates.append(
                {
                    "name": f"{arm}_evidence_visible",
                    "threshold": ">=80%",
                    "actual": f"{visible}/{n} ({vis_pct:.0f}%)",
                    "passed": passed_vis,
                    "details": "Evidence must be visible in >=80% of trajectories",
                }
            )

            errors = sum(1 for r in records if r.hook_error_count > 0)
            passed_err = errors == 0
            if not passed_err:
                all_passed = False
            total_errors = sum(r.hook_error_count for r in records)
            gates.append(
                {
                    "name": f"{arm}_zero_hook_errors",
                    "threshold": "0",
                    "actual": f"{total_errors} errors across {errors} tasks",
                    "passed": passed_err,
                    "details": "No hook errors allowed in GT arms",
                }
            )

    # Cross-arm config parity
    all_hashes: set[str] = set()
    for records in records_by_arm.values():
        for r in records:
            if r.config_hash and r.config_hash != "unknown":
                all_hashes.add(r.config_hash)
    if len(all_hashes) > 1:
        all_passed = False
        gates.append(
            {
                "name": "cross_arm_config_parity",
                "threshold": "1 unique config hash",
                "actual": f"{len(all_hashes)} unique hashes: {sorted(all_hashes)}",
                "passed": False,
                "details": "All arms must share the same base config",
            }
        )
    elif all_hashes:
        gates.append(
            {
                "name": "cross_arm_config_parity",
                "threshold": "1 unique config hash",
                "actual": f"1 ({next(iter(all_hashes))})",
                "passed": True,
                "details": "All arms share the same base config",
            }
        )

    return GateResult(passed=all_passed, gates=gates)


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_gate_report(result: GateResult) -> str:
    """Format a GateResult as a markdown report."""
    lines: list[str] = []
    lines.append("# Evidence Delivery Gate Report")
    lines.append("")
    lines.append("| Gate | Threshold | Actual | Result |")
    lines.append("|------|-----------|--------|--------|")
    for g in result.gates:
        status = "PASS" if g["passed"] else "FAIL"
        lines.append(f"| {g['name']} | {g['threshold']} | {g['actual']} | {status} |")
    lines.append("")
    verdict = "PASS" if result.passed else "FAIL"
    lines.append(f"VERDICT: {verdict}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI: scan
# ---------------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> int:
    """Execute the scan subcommand."""
    task_ids: list[str] | None = None
    if args.instances:
        task_ids = [t.strip() for t in args.instances.split(",") if t.strip()]

    all_records: list[TelemetryRecord] = []

    arm_specs: list[tuple[str, str, Path | None, Path | None]] = []
    # (arm_name, gt_version, output_dir, log_dir)

    if args.baseline:
        arm_specs.append(("baseline", "none", Path(args.baseline), None))
    if args.gt_v11:
        log_dir = Path(args.gt_v11_logs) if args.gt_v11_logs else None
        arm_specs.append(("gt_v11", "v1.1", Path(args.gt_v11), log_dir))
    if args.gt_v13:
        log_dir = Path(args.gt_v13_logs) if args.gt_v13_logs else None
        arm_specs.append(("gt_v13", "v1.3", Path(args.gt_v13), log_dir))

    if not arm_specs:
        print("ERROR: at least one arm (--baseline, --gt-v11, --gt-v13) required",
              file=sys.stderr)
        return 1

    model = args.model or "unknown"

    for arm_name, gt_version, output_dir, log_dir in arm_specs:
        output_jsonl = None
        if output_dir:
            # OH nests output: <dir>/<dataset>/<agent>/<model>/output.jsonl
            candidates = list(output_dir.rglob("output.jsonl"))
            if candidates:
                output_jsonl = candidates[0]
            elif (output_dir / "output.jsonl").exists():
                output_jsonl = output_dir / "output.jsonl"

        # Discover task IDs from output.jsonl if not provided
        effective_ids = task_ids
        if effective_ids is None and output_jsonl and output_jsonl.exists():
            effective_ids = []
            with open(output_jsonl, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        iid = entry.get("instance_id", "")
                        if iid:
                            effective_ids.append(iid)
                    except json.JSONDecodeError:
                        continue

        if not effective_ids:
            print(f"WARNING: no task IDs found for arm {arm_name}", file=sys.stderr)
            continue

        # Compute config hash
        metadata_path = output_dir / "METADATA.json" if output_dir else None
        cfg_hash = hash_config(metadata_path)

        # Scan hook logs (GT arms only)
        if arm_name == "baseline":
            hook_data: dict[str, TelemetryRecord] = {
                tid: TelemetryRecord(task_id=tid, arm=arm_name, gt_version=gt_version)
                for tid in effective_ids
            }
        else:
            hook_data = scan_hook_logs(log_dir, arm_name, gt_version, effective_ids)

        # Scan trajectory
        traj_data: dict[str, dict[str, Any]] = {}
        if output_jsonl and output_jsonl.exists():
            traj_data = scan_trajectory(
                output_jsonl, arm_name, gt_version, effective_ids
            )

        # Merge
        records = merge_telemetry(hook_data, traj_data, model, cfg_hash)
        all_records.extend(records)

    # Write telemetry
    out_path = Path(args.telemetry_out)
    with open(out_path, "w", encoding="utf-8") as fh:
        for rec in all_records:
            fh.write(json.dumps(asdict(rec), separators=(",", ":")) + "\n")

    print(f"Wrote {len(all_records)} telemetry records to {out_path}")
    return 0


# ---------------------------------------------------------------------------
# CLI: verify
# ---------------------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    """Execute the verify subcommand."""
    telemetry_path = Path(args.telemetry)
    if not telemetry_path.exists():
        print(f"ERROR: telemetry file not found: {telemetry_path}", file=sys.stderr)
        return 1

    # Load records
    records_by_arm: dict[str, list[TelemetryRecord]] = {}
    with open(telemetry_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec = TelemetryRecord(**{
                k: v for k, v in d.items() if k in TelemetryRecord.__dataclass_fields__
            })
            records_by_arm.setdefault(rec.arm, []).append(rec)

    if not records_by_arm:
        print("ERROR: no telemetry records found", file=sys.stderr)
        return 1

    # Check gates
    result = check_gates(records_by_arm)
    report = format_gate_report(result)

    # Print to stdout
    print(report)

    # Write to file
    if args.gate_report:
        report_path = Path(args.gate_report)
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"Gate report written to {report_path}")

    return 0 if result.passed else 1


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evidence Delivery Smoke Gate for OpenHands",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # scan
    p_scan = sub.add_parser("scan", help="Scan hook logs + trajectories, emit telemetry")
    p_scan.add_argument("--baseline", type=str, default=None,
                        help="Baseline arm output directory")
    p_scan.add_argument("--gt-v11", type=str, default=None,
                        help="GT v1.1 arm output directory")
    p_scan.add_argument("--gt-v11-logs", type=str, default=None,
                        help="GT v1.1 hook log directory")
    p_scan.add_argument("--gt-v13", type=str, default=None,
                        help="GT v1.3 arm output directory")
    p_scan.add_argument("--gt-v13-logs", type=str, default=None,
                        help="GT v1.3 hook log directory")
    p_scan.add_argument("--instances", type=str, default=None,
                        help="Comma-separated task IDs (auto-discovered if omitted)")
    p_scan.add_argument("--model", type=str, default="unknown",
                        help="Model name for telemetry")
    p_scan.add_argument("--telemetry-out", type=str,
                        default="delivery_telemetry.jsonl",
                        help="Output telemetry JSONL path")

    # verify
    p_verify = sub.add_parser("verify", help="Verify delivery gates from telemetry")
    p_verify.add_argument("--telemetry", type=str, required=True,
                          help="Input telemetry JSONL path")
    p_verify.add_argument("--gate-report", type=str, default=None,
                          help="Output gate report markdown path")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "scan":
        return cmd_scan(args)
    elif args.command == "verify":
        return cmd_verify(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
