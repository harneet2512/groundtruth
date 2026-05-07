from __future__ import annotations

import pytest

from groundtruth.formatters import format_briefing, format_context, format_impact, format_json
from groundtruth.models import AffectedSymbol, Behavior, Briefing, Caller, ContextResult, Impact, Rule


def _sample_briefing() -> Briefing:
    caller = Caller(
        symbol="alpha",
        file="a.py",
        line=12,
        resolution_method="import",
        is_deterministic=True,
    )
    return Briefing(
        symbol="beta",
        file="b.py",
        callers=[caller],
        behaviors=[],
        rules=[],
        evidence_text="",
        confidence="high",
    )


def test_format_briefing_markdown_xml_plain() -> None:
    b = _sample_briefing()
    md = format_briefing(b, "markdown")
    assert "beta" in md
    assert "alpha" in md
    xml = format_briefing(b, "xml")
    assert "<gt-evidence" in xml
    plain = format_briefing(b, "plain")
    assert "Briefing:" in plain


def test_format_briefing_with_behaviors_rules_all_formats() -> None:
    caller = Caller("c", "f.py", 1, "import", True)
    b = Briefing(
        symbol="s",
        file="sf.py",
        callers=[caller],
        behaviors=[Behavior(kind="prefer", target="retry")],
        rules=[Rule(pattern="*", prevalence=1.0, source_count=1)],
        evidence_text="",
        confidence="medium",
    )
    md = format_briefing(b, "markdown")
    assert "### Behaviors" in md and "### Rules" in md
    xml = format_briefing(b, "xml")
    assert "<behavior" in xml and "<rule " in xml
    plain = format_briefing(b, "plain")
    assert "Behavior:" in plain and "Rule:" in plain


def test_format_briefing_empty_callers_block() -> None:
    b = Briefing(
        symbol="solo",
        file="solo.py",
        callers=[],
        behaviors=[],
        rules=[],
        evidence_text="",
        confidence="low",
    )
    xml = format_briefing(b, "xml")
    assert "<none/>" in xml
    md = format_briefing(b, "markdown")
    assert "(none)" in md


def test_format_impact_with_diff_block() -> None:
    br = Caller(symbol="brk", file="bf.py", line=9, resolution_method="name_match", is_deterministic=False)
    impact = Impact(
        affected_symbols=[AffectedSymbol(symbol="s", file="f.py", relationship="direct_caller")],
        breaking_callers=[br],
        rule_violations=[Rule(pattern="*", prevalence=1.0, source_count=1)],
        summary="ok",
    )
    md = format_impact(impact, "markdown", diff_block="+line")
    assert "ok" in md
    assert "Diff" in md and "brk" in md
    plain = format_impact(impact, "plain", diff_block="@@")
    assert "Diff (context):" in plain
    xml = format_impact(impact, "xml", diff_block="patch")
    assert "<diff>" in xml and "patch" in xml


def test_format_impact_empty_sections() -> None:
    impact = Impact(affected_symbols=[], breaking_callers=[], rule_violations=[], summary="nada")
    xml = format_impact(impact, "xml")
    assert 'kind="impact"' in xml


def test_format_context_plain_footer() -> None:
    caller = Caller(
        symbol="c",
        file="f.py",
        line=1,
        resolution_method="same_file",
        is_deterministic=True,
    )
    ctx = ContextResult(matches=[caller], call_graph={"seed": ["c"]}, evidence="trail")
    out = format_context(ctx, "plain", seed_symbol="seed")
    assert "trail" in out


def test_format_context_markdown_and_xml_shapes() -> None:
    caller = Caller("sym", "g.py", 2, "import", True)
    ctx_empty = ContextResult(matches=[], call_graph={}, evidence="noop")
    md_empty = format_context(ctx_empty, "markdown", seed_symbol="seed")
    assert "(none)" in md_empty and "(empty)" in md_empty
    ctx = ContextResult(matches=[caller], call_graph={"seed": ["sym"]}, evidence="trail")
    md = format_context(ctx, "markdown", seed_symbol="seed")
    assert "sym" in md
    xml = format_context(ctx, "xml", seed_symbol="seed")
    assert "<match " in xml and "<call_graph>" in xml


def test_format_context_plain_without_evidence_footer() -> None:
    caller = Caller("sym", "g.py", 2, "import", True)
    ctx = ContextResult(matches=[caller], call_graph={"seed": ["sym"]}, evidence="")
    out = format_context(ctx, "plain", seed_symbol="seed")
    assert out.endswith("\n")


def test_format_json_typeerror_fallback() -> None:
    class Odd:
        pass

    with pytest.raises(TypeError):
        format_json(Odd())


def test_format_json_roundtrip_dataclass() -> None:
    b = _sample_briefing()
    dumped = format_json(b)
    assert "\"symbol\"" in dumped
    assert "beta" in dumped
