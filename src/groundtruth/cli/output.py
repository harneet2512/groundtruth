"""Shared risk summary renderer for CLI commands."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

from groundtruth.analysis.risk_scorer import RiskScore


def _is_tty() -> bool:
    """Check if stdout is a terminal."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


@dataclass
class _Colors:
    """ANSI escape sequences for terminal output."""

    bold: str
    dim: str
    reset: str
    green: str
    yellow: str
    red: str
    orange: str

    @classmethod
    def for_tty(cls, is_tty: bool) -> _Colors:
        if not is_tty:
            return cls(bold="", dim="", reset="", green="", yellow="", red="", orange="")
        return cls(
            bold="\033[1m",
            dim="\033[2m",
            reset="\033[0m",
            green="\033[32m",
            yellow="\033[33m",
            red="\033[31m",
            orange="\033[38;5;208m",
        )


def classify_risk(score_0_to_100: int) -> str:
    """Classify a risk score into a label."""
    if score_0_to_100 <= 25:
        return "LOW"
    if score_0_to_100 <= 50:
        return "MODERATE"
    if score_0_to_100 <= 75:
        return "HIGH"
    return "CRITICAL"


def _risk_color(c: _Colors, label: str) -> str:
    """Get the color for a risk label."""
    if label == "LOW":
        return c.green
    if label == "MODERATE":
        return c.yellow
    if label == "HIGH":
        return c.orange
    return c.red


def _render_bar(counts: dict[str, int], total: int, c: _Colors, width: int = 40) -> list[str]:
    """Render a horizontal risk distribution bar with legend."""
    if total == 0:
        return [" " * width, "  No files scored"]

    order = ["low", "moderate", "high", "critical"]
    color_map = {
        "low": c.green,
        "moderate": c.yellow,
        "high": c.orange,
        "critical": c.red,
    }

    bar_parts: list[str] = []
    legend_parts: list[str] = []

    for level in order:
        cnt = counts.get(level, 0)
        if total > 0:
            segment_width = max(0, round(cnt / total * width))
        else:
            segment_width = 0
        if cnt > 0 and segment_width == 0:
            segment_width = 1
        color = color_map[level]
        bar_parts.append(f"{color}{'█' * segment_width}{c.reset}")
        legend_parts.append(f"{color}■{c.reset} {cnt} {level}")

    bar_line = "".join(bar_parts)
    legend_line = "  " + "  ".join(legend_parts)
    return [f"  {bar_line}", legend_line]


def _truncate(path: str, max_len: int = 40) -> str:
    """Truncate a path to max_len, adding ... prefix if needed."""
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3) :]


def _compute_risk_distribution(risk_scores: list[RiskScore]) -> dict[str, int]:
    """Count files in each risk bucket."""
    counts: dict[str, int] = {"low": 0, "moderate": 0, "high": 0, "critical": 0}
    for rs in risk_scores:
        score_100 = int(rs.overall_risk * 100)
        label = classify_risk(score_100).lower()
        counts[label] = counts.get(label, 0) + 1
    return counts


def _compute_overall_score(risk_scores: list[RiskScore]) -> int:
    """Compute a single overall risk score (0-100) from all file scores."""
    if not risk_scores:
        return 0
    avg = sum(rs.overall_risk for rs in risk_scores) / len(risk_scores)
    return int(avg * 100)


def render_risk_summary(
    project_name: str,
    stats: dict[str, object],
    risk_scores: list[RiskScore],
    dead_code_count: int,
    unused_packages_count: int,
    packages_count: int,
    elapsed_seconds: float | None = None,
    command: str = "status",
    is_tty: bool | None = None,
) -> str:
    """Render a formatted risk summary for terminal output."""
    tty = is_tty if is_tty is not None else _is_tty()
    c = _Colors.for_tty(tty)

    lines: list[str] = []

    # Header
    lines.append("")
    lines.append(f"{c.bold}GroundTruth Risk Report{c.reset}  for {project_name}")
    lines.append("")

    # Stats block
    files_count = stats.get("files_count", 0)
    symbols_count = stats.get("symbols_count", 0)
    refs_count = stats.get("refs_count", 0)
    lines.append(f"  Files:      {files_count:,}")
    lines.append(f"  Symbols:    {symbols_count:,}")
    lines.append(f"  References: {refs_count:,}")
    lines.append(f"  Packages:   {packages_count:,}")
    if elapsed_seconds is not None:
        lines.append(f"  Index time: {elapsed_seconds:.1f}s")
    lines.append("")

    # Overall risk score
    overall_score = _compute_overall_score(risk_scores)
    label = classify_risk(overall_score)
    color = _risk_color(c, label)
    lines.append(
        f"  Hallucination risk:  {color}{c.bold}{label}{c.reset}"
        f" {c.dim}(score: {overall_score}/100){c.reset}"
    )
    lines.append("")

    # Distribution bar
    distribution = _compute_risk_distribution(risk_scores)
    bar_lines = _render_bar(distribution, len(risk_scores), c)
    lines.extend(bar_lines)
    lines.append("")

    # Top 5 hotspots
    top_5 = sorted(risk_scores, key=lambda rs: rs.overall_risk, reverse=True)[:5]
    if top_5:
        lines.append(f"  {c.bold}Top hotspots:{c.reset}")
        for rs in top_5:
            score_100 = int(rs.overall_risk * 100)
            top_factor = ""
            if rs.factors:
                top_factor = max(rs.factors, key=rs.factors.get)  # type: ignore[arg-type]
            path_str = _truncate(rs.file_path)
            hl = classify_risk(score_100)
            clr = _risk_color(c, hl)
            lines.append(
                f"    {path_str:<40}  {clr}{score_100:>3}/100{c.reset}"
                f"  {c.dim}{top_factor}{c.reset}"
            )
        lines.append("")

    # Dead code & unused packages
    if dead_code_count > 0:
        lines.append(
            f"  {c.yellow}Dead code:{c.reset} {dead_code_count:,} exported symbols with zero references"
        )
    if unused_packages_count > 0:
        lines.append(
            f"  {c.yellow}Unused packages:{c.reset} {unused_packages_count:,} of {packages_count:,}"
        )
    if dead_code_count > 0 or unused_packages_count > 0:
        lines.append("")

    # Context-dependent suggestions
    if command == "index":
        lines.append(
            f"  {c.dim}Run 'groundtruth status' for details or 'groundtruth viz' for 3D risk map.{c.reset}"
        )
    elif command == "status":
        lines.append(
            f"  {c.dim}Run 'groundtruth viz' for 3D risk map or 'groundtruth risk-map' for full list.{c.reset}"
        )
    elif command == "viz":
        lines.append(
            f"  {c.dim}Run 'groundtruth status --json' for machine-readable output.{c.reset}"
        )
    lines.append("")

    return "\n".join(lines)


def render_status_json(
    project_name: str,
    stats: dict[str, object],
    risk_scores: list[RiskScore],
    dead_code_count: int,
    unused_packages_count: int,
    packages_count: int,
) -> str:
    """Render status as JSON."""
    overall_score = _compute_overall_score(risk_scores)
    distribution = _compute_risk_distribution(risk_scores)

    top_hotspots: list[dict[str, Any]] = []
    sorted_scores = sorted(risk_scores, key=lambda rs: rs.overall_risk, reverse=True)[:10]
    for rs in sorted_scores:
        top_factor = ""
        if rs.factors:
            top_factor = max(rs.factors, key=rs.factors.get)  # type: ignore[arg-type]
        top_hotspots.append(
            {
                "file": rs.file_path,
                "score": int(rs.overall_risk * 100),
                "top_factor": top_factor,
            }
        )

    data: dict[str, Any] = {
        "project": project_name,
        "files": stats.get("files_count", 0),
        "symbols": stats.get("symbols_count", 0),
        "references": stats.get("refs_count", 0),
        "packages": packages_count,
        "risk_score": overall_score,
        "risk_label": classify_risk(overall_score),
        "risk_distribution": distribution,
        "hotspots": top_hotspots,
        "dead_code_count": dead_code_count,
        "unused_packages_count": unused_packages_count,
    }
    return json.dumps(data, indent=2)
