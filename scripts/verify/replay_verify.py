#!/usr/bin/env python3
r"""
Trajectory replay verifier for GroundTruth evidence delivery.

Parses frozen canary run artifacts (output.jsonl + gt_layer_events JSONL)
and compares what GT tried to send vs what the agent actually received.

Usage:
    python scripts/verify/replay_verify.py D:\tmp\canary_phase4
    python scripts/verify/replay_verify.py D:\tmp\canary_phase4 --task loguru-1306
    python scripts/verify/replay_verify.py D:\tmp\canary_phase4 --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class LayerResult:
    """Delivery status for a single layer in a single task."""
    name: str
    delivered: bool = False
    detail: str = ""
    char_count: int = 0
    injection_count: int = 0
    events_generated: int = 0
    markers_found: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def status_str(self) -> str:
        if self.delivered:
            return "DELIVERED"
        elif self.events_generated > 0:
            return "NOT DELIVERED"
        else:
            return "NOT FIRED"

    def summary_line(self) -> str:
        parts = [self.status_str]
        if self.delivered:
            if self.char_count > 0:
                parts.append(f"{self.char_count} chars")
            if self.injection_count > 0:
                parts.append(f"{self.injection_count} injections")
            if self.markers_found:
                parts.append(", ".join(self.markers_found))
            if self.detail:
                parts.append(self.detail)
        else:
            if self.events_generated > 0:
                parts.append(f"{self.events_generated} events generated, 0 in agent history")
            if self.detail:
                parts.append(self.detail)
        return f"{self.name:20s} {' | '.join(parts)}"


@dataclass
class TaskReport:
    """Delivery report for all layers in a single task."""
    task_id: str
    layers: dict[str, LayerResult] = field(default_factory=dict)

    @property
    def delivered_count(self) -> int:
        return sum(1 for lr in self.layers.values() if lr.delivered)

    @property
    def total_layers(self) -> int:
        return len(self.layers)

    @property
    def fired_count(self) -> int:
        """Layers that GT attempted to deliver (events_generated > 0 or delivered)."""
        return sum(
            1 for lr in self.layers.values()
            if lr.delivered or lr.events_generated > 0
        )

    @property
    def broken_count(self) -> int:
        """Layers that fired but were not delivered."""
        return sum(
            1 for lr in self.layers.values()
            if not lr.delivered and lr.events_generated > 0
        )


# ---------------------------------------------------------------------------
# Layer names (DOC_OF_HONOR canonical order)
# ---------------------------------------------------------------------------

LAYER_NAMES = [
    "L1 Brief",
    "L1+ Edit-Target",
    "L3 Post-Edit",
    "L3b Post-View",
    "L5 Governor",
    "L6 Pre-Submit",
    "Grep Intercept",
    "Consensus",
]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def find_output_jsonl(task_dir: Path) -> Path | None:
    """Recursively find output.jsonl under a task directory."""
    for root, _dirs, files in os.walk(task_dir):
        if "output.jsonl" in files:
            return Path(root) / "output.jsonl"
    return None


def find_layer_events(task_dir: Path) -> Path | None:
    """Find gt_layer_events_*.jsonl in gt_debug/."""
    gt_debug = task_dir / "gt_debug"
    if not gt_debug.is_dir():
        return None
    for f in gt_debug.iterdir():
        if f.name.startswith("gt_layer_events") and f.suffix == ".jsonl":
            return f
    return None


def load_history(output_path: Path) -> list[dict]:
    """Parse output.jsonl — a single JSON line containing {history: [...]}."""
    with open(output_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            if "history" in data:
                return data["history"]
    return []


def load_layer_events(events_path: Path) -> list[dict]:
    """Parse gt_layer_events JSONL — one event per line."""
    events = []
    with open(events_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def extract_text(entry: dict) -> str:
    """Extract the full text content from a history entry.

    GT markers can appear in:
    - entry['content'] (observation entries)
    - entry['args']['content'] (action entries like 'message')
    """
    parts = []
    if entry.get("content"):
        parts.append(entry["content"])
    args = entry.get("args", {})
    if isinstance(args, dict) and args.get("content"):
        parts.append(args["content"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Per-layer checkers
# ---------------------------------------------------------------------------

def check_l1_brief(history: list[dict]) -> LayerResult:
    """L1 Brief: find <gt-task-brief> in history."""
    result = LayerResult(name="L1 Brief")
    for entry in history:
        text = extract_text(entry)
        if "<gt-task-brief>" not in text:
            continue
        result.delivered = True
        # Extract brief content
        start = text.find("<gt-task-brief>")
        end = text.find("</gt-task-brief>")
        if end > start:
            brief_text = text[start : end + len("</gt-task-brief>")]
        else:
            brief_text = text[start : start + 3000]
        result.char_count = len(brief_text)

        # Extract listed files (lines starting with number. filepath)
        files_listed = re.findall(r"^\d+\.\s+(\S+)", brief_text, re.MULTILINE)
        result.extra["files_listed"] = files_listed
        result.detail = f"{len(files_listed)} files listed"
        break
    return result


def check_l1_edit_target(history: list[dict]) -> LayerResult:
    """L1+ Edit-Target: find <gt-edit-target> in history."""
    result = LayerResult(name="L1+ Edit-Target")
    for entry in history:
        text = extract_text(entry)
        if "<gt-edit-target>" not in text:
            continue
        result.delivered = True
        start = text.find("<gt-edit-target>")
        end = text.find("</gt-edit-target>")
        if end > start:
            target_text = text[start : end + len("</gt-edit-target>")]
        else:
            target_text = text[start : start + 500]
        result.char_count = len(target_text)

        # Extract function name and file
        fn_match = re.search(
            r"Key function:\s*(\S+)\s+in\s+(\S+)", target_text
        )
        if fn_match:
            func_name = fn_match.group(1).rstrip(",")
            file_path = fn_match.group(2).rstrip(",")
            result.detail = f"{func_name} in {file_path}"
            result.extra["function"] = func_name
            result.extra["file"] = file_path
        break
    return result


def check_l3_post_edit(history: list[dict]) -> LayerResult:
    """L3 Post-Edit: find <gt-evidence trigger="post_edit in history."""
    result = LayerResult(name="L3 Post-Edit")
    post_edit_markers = [
        "[SIGNATURE]",
        "[CONTRACT]",
        "[TEST]",
        "PRESERVE:",
        "[PATTERN]",
        "[PEER]",
        "[CALLER]",
        "[CATCHES]",
        "SEMANTIC WARNING:",
    ]
    injection_count = 0
    total_chars = 0
    all_markers: set[str] = set()

    for entry in history:
        text = extract_text(entry)
        if '<gt-evidence trigger="post_edit' not in text:
            continue
        injection_count += 1
        # Find the evidence block
        idx = text.find('<gt-evidence trigger="post_edit')
        end_tag = text.find("</gt-evidence>", idx)
        if end_tag > idx:
            block = text[idx : end_tag + len("</gt-evidence>")]
        else:
            block = text[idx : idx + 2000]
        total_chars += len(block)
        for marker in post_edit_markers:
            if marker in block:
                all_markers.add(marker)

    if injection_count > 0:
        result.delivered = True
        result.injection_count = injection_count
        result.char_count = total_chars
        result.markers_found = sorted(all_markers)
    return result


def check_l3b_post_view(history: list[dict]) -> LayerResult:
    """L3b Post-View: find [GT] followed by file navigation context.

    Patterns:
    - [GT] filename:\n  Called by: ...
    - [GT] filename:\n  Calls into: ...
    """
    result = LayerResult(name="L3b Post-View")
    injection_count = 0
    total_chars = 0
    files_with_evidence: set[str] = set()

    for entry in history:
        text = extract_text(entry)
        # Match [GT] <name>:\n with Called by: or Calls into: nearby
        # But NOT [GT] Callers of (that's grep intercept) and NOT [GT] Post-edit
        # and NOT [GT L5 (that's governor)
        matches = list(re.finditer(
            r"\[GT\]\s+([A-Za-z0-9_./]+):\s*\n",
            text,
        ))
        for m in matches:
            file_ref = m.group(1)
            # Skip if this is part of a grep intercept or L5
            prefix_start = max(0, m.start() - 5)
            prefix = text[prefix_start : m.start()]
            if "L5" in prefix:
                continue
            # Check context after the match for caller/callee evidence
            after = text[m.end() : m.end() + 500]
            if "Called by:" in after or "Calls into:" in after or "[CATCHES]" in after:
                injection_count += 1
                files_with_evidence.add(file_ref)
                # Estimate char count for this block
                next_blank = after.find("\n\n")
                if next_blank > 0:
                    total_chars += m.end() - m.start() + next_blank
                else:
                    total_chars += 200

    if injection_count > 0:
        result.delivered = True
        result.injection_count = injection_count
        result.char_count = total_chars
        result.extra["files"] = sorted(files_with_evidence)
        file_list = ", ".join(sorted(files_with_evidence))
        result.detail = f"callers/callees for {file_list}"
    return result


def check_l5_governor(history: list[dict]) -> LayerResult:
    """L5 Governor: find [GT L5 or 'No Source Edits' in history."""
    result = LayerResult(name="L5 Governor")
    for i, entry in enumerate(history):
        text = extract_text(entry)
        if "[GT L5" not in text:
            continue
        result.delivered = True
        # Extract iteration info
        iter_match = re.search(r"Iteration:\s*(\d+)/(\d+)", text)
        if iter_match:
            iteration = int(iter_match.group(1))
            max_iter = int(iter_match.group(2))
            result.extra["iteration"] = iteration
            result.extra["max_iter"] = max_iter
            result.detail = f"iter {iteration}/{max_iter}"
        # Determine variant
        if "No Source Edits" in text:
            variant = "No Source Edits"
        elif "scaffolding" in text.lower():
            variant = "Scaffolding Trap"
        else:
            variant = "unknown"
        result.extra["variant"] = variant
        if variant != "unknown":
            result.detail += f", \"{variant}\""
        # Character count of the GT L5 block
        idx = text.find("[GT L5")
        block_end = text.find("\n\n", idx)
        if block_end < 0:
            block_end = len(text)
        result.char_count = block_end - idx
        break
    return result


def check_l6_pre_submit(history: list[dict]) -> LayerResult:
    """L6 Pre-Submit: find PRE-SUBMIT or [PRE-SUBMIT REVIEW] in history."""
    result = LayerResult(name="L6 Pre-Submit")
    for entry in history:
        text = extract_text(entry)
        if "PRE-SUBMIT" in text or "pre-submit" in text.lower():
            result.delivered = True
            idx = text.upper().find("PRE-SUBMIT")
            result.char_count = len(text[idx : idx + 500])
            break
    return result


def check_grep_intercept(history: list[dict]) -> LayerResult:
    """Grep Intercept: find [GT] Callers of in history."""
    result = LayerResult(name="Grep Intercept")
    injection_count = 0
    total_chars = 0
    symbols: set[str] = set()

    for entry in history:
        text = extract_text(entry)
        for m in re.finditer(r"\[GT\] Callers of '([^']+)':", text):
            injection_count += 1
            symbols.add(m.group(1))
            # Estimate block size
            block_end = text.find("\n\n", m.end())
            if block_end < 0:
                block_end = m.end() + 200
            total_chars += block_end - m.start()

    if injection_count > 0:
        result.delivered = True
        result.injection_count = injection_count
        result.char_count = total_chars
        sym_list = ", ".join(sorted(symbols))
        result.detail = f"symbols: {sym_list}"
    return result


def check_consensus(history: list[dict]) -> LayerResult:
    """Consensus: find <gt-scope in history."""
    result = LayerResult(name="Consensus")
    for entry in history:
        text = extract_text(entry)
        if "<gt-scope" not in text:
            continue
        result.delivered = True
        idx = text.find("<gt-scope")
        end_tag = text.find("</gt-scope>", idx)
        if end_tag > idx:
            block = text[idx : end_tag + len("</gt-scope>")]
        else:
            block = text[idx : idx + 500]
        result.char_count = len(block)
        # Extract file count from files="N" attribute
        files_attr = re.search(r'files="(\d+)"', block)
        if files_attr:
            result.detail = f"{files_attr.group(1)} files in scope"
        break
    # Count all injections across history
    if result.delivered:
        count = 0
        for e in history:
            t = extract_text(e)
            count += len(re.findall(r"<gt-scope", t))
        result.injection_count = count
    return result


# ---------------------------------------------------------------------------
# Event counting
# ---------------------------------------------------------------------------

# Mapping from gt_layer_events layer names to our DOC_OF_HONOR names
LAYER_EVENT_MAP = {
    # L1
    ("L1", "localization_brief"): "L1 Brief",
    # L1+ edit-target is part of L1 brief emission, no separate event
    # L3 post-edit
    ("L3_router_v2", "on_edit"): "L3 Post-Edit",
    # L3b post-view
    ("L3_router_v2", "on_view"): "L3b Post-View",
    # L5 governor
    ("L5", "multi_file_scope_warning"): "L5 Governor",
    ("L5", "scaffolding_trap_early"): "L5 Governor",
    ("L5b", "intervention_multi_file_scope_warning"): "L5 Governor",
    ("L5b", "intervention_scaffolding_trap_early"): "L5 Governor",
    # L6 pre-submit
    ("L6", "pre_submit_review"): "L6 Pre-Submit",
    # L6 reindex is infrastructure, not agent-facing evidence
    # Grep intercept and Consensus are in-container hooks, no separate events
}


def count_events_per_layer(events: list[dict]) -> dict[str, int]:
    """Count emitted events per DOC_OF_HONOR layer name."""
    counts: dict[str, int] = {name: 0 for name in LAYER_NAMES}
    for ev in events:
        if not ev.get("emitted", False):
            continue
        key = (ev.get("layer", ""), ev.get("event_type", ""))
        doc_name = LAYER_EVENT_MAP.get(key)
        if doc_name:
            counts[doc_name] += 1
    return counts


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze_task(task_dir: Path) -> TaskReport | None:
    """Analyze a single task directory and return a delivery report."""
    task_name = task_dir.name
    # Extract task_id from directory name (strip canary-v2_live- prefix)
    task_id = task_name
    prefix = "canary-v2_live-"
    if task_id.startswith(prefix):
        task_id = task_id[len(prefix):]

    output_path = find_output_jsonl(task_dir)
    if output_path is None:
        print(f"WARNING: No output.jsonl found in {task_dir}", file=sys.stderr)
        return None

    events_path = find_layer_events(task_dir)

    # Load data
    history = load_history(output_path)
    if not history:
        print(f"WARNING: Empty history in {output_path}", file=sys.stderr)
        return None

    events = load_layer_events(events_path) if events_path else []
    event_counts = count_events_per_layer(events)

    # Run all layer checkers
    report = TaskReport(task_id=task_id)
    checkers = [
        check_l1_brief,
        check_l1_edit_target,
        check_l3_post_edit,
        check_l3b_post_view,
        check_l5_governor,
        check_l6_pre_submit,
        check_grep_intercept,
        check_consensus,
    ]
    for checker in checkers:
        layer_result = checker(history)
        # Merge event counts
        layer_result.events_generated = event_counts.get(layer_result.name, 0)
        report.layers[layer_result.name] = layer_result

    return report


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_task_report(report: TaskReport) -> None:
    """Print per-task delivery report."""
    print(f"\n{'=' * 60}")
    print(f"=== {report.task_id}")
    print(f"{'=' * 60}")
    for name in LAYER_NAMES:
        lr = report.layers.get(name)
        if lr is None:
            print(f"  {name:20s} UNKNOWN")
        else:
            print(f"  {lr.summary_line()}")

    delivered = report.delivered_count
    fired = report.fired_count
    broken = report.broken_count
    total = report.total_layers
    print(f"\n  SUMMARY: {delivered}/{total} layers delivered", end="")
    if broken > 0:
        print(f", {broken}/{total} broken (fired but not delivered)", end="")
    print()


def print_cross_task_summary(reports: list[TaskReport]) -> None:
    """Print cross-task summary table."""
    print(f"\n{'=' * 80}")
    print("CROSS-TASK DELIVERY SUMMARY")
    print(f"{'=' * 80}")

    # Header
    short_names = {
        "L1 Brief": "L1",
        "L1+ Edit-Target": "L1+",
        "L3 Post-Edit": "L3",
        "L3b Post-View": "L3b",
        "L5 Governor": "L5",
        "L6 Pre-Submit": "L6",
        "Grep Intercept": "Grep",
        "Consensus": "Scope",
    }
    header_cols = [f"{short_names.get(n, n):>6s}" for n in LAYER_NAMES]
    print(f"  {'Task':<40s} {''.join(header_cols)}  Score")
    print(f"  {'-' * 40} {'------' * len(LAYER_NAMES)}  -----")

    total_delivered = 0
    total_fired = 0
    total_possible = 0

    for report in reports:
        row = []
        for name in LAYER_NAMES:
            lr = report.layers.get(name)
            if lr is None:
                row.append("  ?   ")
            elif lr.delivered:
                row.append("  YES ")
                total_delivered += 1
            elif lr.events_generated > 0:
                row.append(" LOST ")
                total_fired += 1
            else:
                row.append("  --  ")
        score = f"{report.delivered_count}/{report.total_layers}"
        task_display = report.task_id[:40]
        print(f"  {task_display:<40s} {''.join(row)}  {score}")
        total_possible += report.total_layers

    print(f"  {'-' * 40} {'------' * len(LAYER_NAMES)}  -----")
    # Totals
    total_not_fired = total_possible - total_delivered - total_fired
    print(
        f"  {'TOTALS':<40s} "
        f"Delivered: {total_delivered}/{total_possible}  "
        f"Lost: {total_fired}  "
        f"Not fired: {total_not_fired}"
    )

    # Delivery rate per layer
    print(f"\nPer-layer delivery rate:")
    for name in LAYER_NAMES:
        delivered_count = sum(
            1 for r in reports if r.layers.get(name, LayerResult(name)).delivered
        )
        fired_count = sum(
            1
            for r in reports
            if r.layers.get(name, LayerResult(name)).events_generated > 0
            or r.layers.get(name, LayerResult(name)).delivered
        )
        total = len(reports)
        if fired_count > 0:
            rate = delivered_count / fired_count * 100
            print(
                f"  {name:20s}  {delivered_count}/{fired_count} fired "
                f"({rate:.0f}% delivery rate), "
                f"{fired_count}/{total} tasks fired"
            )
        else:
            print(f"  {name:20s}  0/{total} tasks fired")


def export_json(reports: list[TaskReport]) -> str:
    """Export reports as JSON for programmatic consumption."""
    output = []
    for report in reports:
        task_data = {
            "task_id": report.task_id,
            "delivered_count": report.delivered_count,
            "total_layers": report.total_layers,
            "fired_count": report.fired_count,
            "broken_count": report.broken_count,
            "layers": {},
        }
        for name, lr in report.layers.items():
            task_data["layers"][name] = {
                "status": lr.status_str,
                "delivered": lr.delivered,
                "char_count": lr.char_count,
                "injection_count": lr.injection_count,
                "events_generated": lr.events_generated,
                "markers_found": lr.markers_found,
                "detail": lr.detail,
                "extra": lr.extra,
            }
        output.append(task_data)
    return json.dumps(output, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay-verify GT evidence delivery against frozen canary artifacts."
    )
    parser.add_argument(
        "canary_dir",
        type=Path,
        help="Path to canary run directory (e.g. D:\\tmp\\canary_phase4)",
    )
    parser.add_argument(
        "--task",
        type=str,
        default=None,
        help="Filter to a specific task (substring match on directory name)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of text",
    )
    args = parser.parse_args()

    canary_dir = args.canary_dir
    if not canary_dir.is_dir():
        print(f"ERROR: {canary_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Discover task directories
    task_dirs = sorted(
        p for p in canary_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )

    if args.task:
        task_dirs = [d for d in task_dirs if args.task in d.name]

    if not task_dirs:
        print("ERROR: No task directories found", file=sys.stderr)
        sys.exit(1)

    # Analyze each task
    reports: list[TaskReport] = []
    for task_dir in task_dirs:
        report = analyze_task(task_dir)
        if report:
            reports.append(report)

    if not reports:
        print("ERROR: No reports generated", file=sys.stderr)
        sys.exit(1)

    # Output
    if args.json:
        print(export_json(reports))
    else:
        for report in reports:
            print_task_report(report)
        if len(reports) > 1:
            print_cross_task_summary(reports)


if __name__ == "__main__":
    main()
