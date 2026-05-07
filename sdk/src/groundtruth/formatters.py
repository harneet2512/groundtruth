"""Output formatters: markdown, XML (``<gt-evidence>``), plain text."""

from __future__ import annotations

import json
import textwrap
from dataclasses import asdict, is_dataclass
from typing import Any, Literal, cast

from groundtruth.models import Briefing, ContextResult, Impact

FormatName = Literal["markdown", "xml", "plain"]

__all__ = [
    "FormatName",
    "format_briefing",
    "format_impact",
    "format_context",
    "format_json",
]


def _esc_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def format_briefing(b: Briefing, fmt: FormatName) -> str:
    if fmt == "markdown":
        lines = [
            f"## Briefing: `{b.symbol}`",
            f"**File:** `{b.file}`",
            f"**Confidence:** {b.confidence}",
            "",
            "### Callers",
        ]
        if not b.callers:
            lines.append("- (none)")
        else:
            for c in b.callers:
                det = "deterministic" if c.is_deterministic else "non-deterministic"
                lines.append(
                    f"- `{c.symbol}` — {c.file}:{c.line} "
                    f"(`{c.resolution_method}`, {det})"
                )
        if b.behaviors:
            lines += ["", "### Behaviors"]
            for be in b.behaviors:
                lines.append(f"- **{be.kind}** → `{be.target}`")
        if b.rules:
            lines += ["", "### Rules"]
            for r in b.rules:
                lines.append(
                    f"- `{r.pattern}` — prevalence {r.prevalence:.2f} (n={r.source_count})"
                )
        return "\n".join(lines).rstrip() + "\n"

    if fmt == "xml":
        parts = [
            f'<gt-evidence symbol="{_esc_xml(b.symbol)}">',
            f"  <file>{_esc_xml(b.file)}</file>",
            f"  <confidence>{_esc_xml(b.confidence)}</confidence>",
            "  <callers>",
        ]
        if not b.callers:
            parts.append("    <none/>")
        else:
            for c in b.callers:
                parts.append(
                    "    <caller "
                    f'symbol="{_esc_xml(c.symbol)}" file="{_esc_xml(c.file)}" '
                    f'line="{c.line}" method="{_esc_xml(c.resolution_method)}" '
                    f'deterministic="{str(c.is_deterministic).lower()}"/>'
                )
        parts.append("  </callers>")
        if b.behaviors:
            parts.append("  <behaviors>")
            for be in b.behaviors:
                parts.append(
                    f'    <behavior kind="{_esc_xml(be.kind)}" target="{_esc_xml(be.target)}"/>'
                )
            parts.append("  </behaviors>")
        if b.rules:
            parts.append("  <rules>")
            for r in b.rules:
                parts.append(
                    "    <rule "
                    f'pattern="{_esc_xml(r.pattern)}" prevalence="{r.prevalence:.4f}" '
                    f'count="{r.source_count}"/>'
                )
            parts.append("  </rules>")
        parts.append("</gt-evidence>")
        return "\n".join(parts) + "\n"

    # plain
    lines = [
        f"Briefing: {b.symbol}",
        f"File: {b.file}",
        f"Confidence: {b.confidence}",
        "Callers:",
    ]
    if not b.callers:
        lines.append("  (none)")
    else:
        for c in b.callers:
            lines.append(
                f"  - {c.symbol} @ {c.file}:{c.line} "
                f"({c.resolution_method}, deterministic={c.is_deterministic})"
            )
    for be in b.behaviors:
        lines.append(f"Behavior: {be.kind} -> {be.target}")
    for r in b.rules:
        lines.append(f"Rule: {r.pattern} prevalence={r.prevalence:.2f} n={r.source_count}")
    return "\n".join(lines).rstrip() + "\n"


def format_impact(i: Impact, fmt: FormatName, *, diff_block: str | None = None) -> str:
    diff_block = diff_block or ""
    if fmt == "markdown":
        lines = [
            "## Impact",
            f"**Summary:** {i.summary}",
            "",
            "### Affected symbols",
        ]
        if not i.affected_symbols:
            lines.append("- (none)")
        else:
            for a in i.affected_symbols:
                lines.append(f"- `{a.symbol}` — `{a.file}` ({a.relationship})")
        lines += ["", "### Breaking-risk callers"]
        if not i.breaking_callers:
            lines.append("- (none)")
        else:
            for c in i.breaking_callers:
                lines.append(f"- `{c.symbol}` — {c.file}:{c.line} (`{c.resolution_method}`)")
        lines += ["", "### Rule violations"]
        if not i.rule_violations:
            lines.append("- (none)")
        else:
            for r in i.rule_violations:
                lines.append(f"- `{r.pattern}`")
        if diff_block.strip():
            lines += ["", "### Diff (context)", "", "```diff", diff_block.rstrip("\n"), "```", ""]
        return "\n".join(lines).rstrip() + "\n"

    if fmt == "xml":
        parts = [
            '<gt-evidence kind="impact">',
            f"  <summary>{_esc_xml(i.summary)}</summary>",
            "  <affected_symbols>",
        ]
        for a in i.affected_symbols:
            parts.append(
                "    <symbol "
                f'name="{_esc_xml(a.symbol)}" file="{_esc_xml(a.file)}" '
                f'relationship="{_esc_xml(a.relationship)}"/>'
            )
        parts += ["  </affected_symbols>", "  <breaking_callers>"]
        for c in i.breaking_callers:
            parts.append(
                "    <caller "
                f'symbol="{_esc_xml(c.symbol)}" file="{_esc_xml(c.file)}" '
                f'line="{c.line}" method="{_esc_xml(c.resolution_method)}"/>'
            )
        parts += ["  </breaking_callers>", "  <rule_violations>"]
        for r in i.rule_violations:
            parts.append(
                f'    <rule pattern="{_esc_xml(r.pattern)}" prevalence="{r.prevalence:.4f}"/>'
            )
        parts += ["  </rule_violations>"]
        if diff_block.strip():
            parts += ["  <diff>", textwrap.indent(_esc_xml(diff_block), "    "), "  </diff>"]
        parts.append("</gt-evidence>")
        return "\n".join(parts) + "\n"

    lines = [f"Impact summary: {i.summary}", "Affected symbols:"]
    if not i.affected_symbols:
        lines.append("  (none)")
    else:
        for a in i.affected_symbols:
            lines.append(f"  - {a.symbol} @ {a.file} ({a.relationship})")
    lines.append("Breaking-risk callers:")
    if not i.breaking_callers:
        lines.append("  (none)")
    else:
        for c in i.breaking_callers:
            lines.append(f"  - {c.symbol} @ {c.file}:{c.line} ({c.resolution_method})")
    lines.append("Rule violations:")
    if not i.rule_violations:
        lines.append("  (none)")
    else:
        for r in i.rule_violations:
            lines.append(f"  - {r.pattern}")
    if diff_block.strip():
        lines += ["Diff (context):", diff_block.rstrip("\n")]
    return "\n".join(lines).rstrip() + "\n"


def format_context(c: ContextResult, fmt: FormatName, *, seed_symbol: str) -> str:
    if fmt == "markdown":
        lines = [
            f"## Context: `{seed_symbol}`",
            "",
            "### Matches (call sites / edges)",
        ]
        if not c.matches:
            lines.append("- (none)")
        else:
            for m in c.matches:
                lines.append(
                    f"- `{m.symbol}` — {m.file}:{m.line} (`{m.resolution_method}`)"
                )
        lines += ["", "### Call graph (adjacency)"]
        if not c.call_graph:
            lines.append("- (empty)")
        else:
            for k in sorted(c.call_graph.keys()):
                targets = ", ".join(f"`{t}`" for t in c.call_graph[k])
                lines.append(f"- `{k}` → {targets}")
        lines += ["", "### Evidence", "", c.evidence.strip(), ""]
        return "\n".join(lines).rstrip() + "\n"

    if fmt == "xml":
        parts = [
            f'<gt-evidence kind="context" symbol="{_esc_xml(seed_symbol)}">',
            "  <matches>",
        ]
        for m in c.matches:
            parts.append(
                "    <match "
                f'symbol="{_esc_xml(m.symbol)}" file="{_esc_xml(m.file)}" '
                f'line="{m.line}" method="{_esc_xml(m.resolution_method)}"/>'
            )
        parts += ["  </matches>", "  <call_graph>"]
        for k in sorted(c.call_graph.keys()):
            joined = ";".join(_esc_xml(t) for t in c.call_graph[k])
            parts.append(f'    <node name="{_esc_xml(k)}" callees="{joined}"/>')
        parts += ["  </call_graph>", f"  <evidence>{_esc_xml(c.evidence.strip())}</evidence>", "</gt-evidence>"]
        return "\n".join(parts) + "\n"

    lines = [f"Context: {seed_symbol}", "Matches:"]
    if not c.matches:
        lines.append("  (none)")
    else:
        for m in c.matches:
            lines.append(f"  - {m.symbol} @ {m.file}:{m.line} ({m.resolution_method})")
    lines.append("Call graph:")
    for k in sorted(c.call_graph.keys()):
        lines.append(f"  {k} -> [{', '.join(c.call_graph[k])}]")
    footer = (c.evidence or "").strip()
    if footer:
        lines += ["Evidence:", footer]
    return "\n".join(lines).rstrip() + "\n"


def format_json(obj: object) -> str:
    """JSON stringify dataclasses / lists / dicts (CLI helper)."""

    def _default(o: object) -> object:
        if isinstance(o, type):
            raise TypeError
        if not is_dataclass(o):
            raise TypeError
        return asdict(cast(Any, o))

    return json.dumps(obj, indent=2, default=_default, sort_keys=True) + "\n"
