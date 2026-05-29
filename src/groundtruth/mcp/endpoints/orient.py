"""groundtruth_orient — Task/file orientation with confidence gating.

Absorbs: orient, find_relevant, hotspots, symbols, dead_code, unused_packages.
Agent asks: "What's relevant here?"
Token budget: 400. Confidence floor: 0.7. Silent when nothing passes gate.
"""

from __future__ import annotations

import re

from groundtruth.index.graph import ImportGraph
from groundtruth.index.graph_store import GraphStore
from groundtruth.mcp.endpoints._contract import contract_line_for
from groundtruth.schema.finding import (
    AgentAction,
    Finding,
    FindingKind,
    Location,
    Severity,
    WhyNow,
    enforce_budget,
    format_findings,
)
from groundtruth.schema.novelty import NoveltyFilter
from groundtruth.schema.pruning import prune_findings
from groundtruth.utils.result import Err

MIN_CONFIDENCE = 0.7
TOKEN_BUDGET = 400
MAX_LOCALIZED = 5
MAX_CALLERS_PER_SYMBOL = 2
MAX_HOTSPOTS = 5

# --- Layer classification (P22) ---
# Research: architectural layer detection — classifying files into architectural
# layers (controller, service, repository, model, etc.) enables the orient
# handler to annotate file listings with layer context, helping the agent
# understand the codebase's structure at a glance.

LAYER_PATTERNS: dict[str, list[str]] = {
    "controller": ["controller", "handler", "endpoint", "view", "route", "api"],
    "service": ["service", "usecase", "interactor", "manager"],
    "repository": ["repository", "repo", "dao", "store", "database"],
    "model": ["model", "entity", "schema", "types", "domain"],
    "middleware": ["middleware", "interceptor", "filter", "pipe"],
    "config": ["config", "settings", "env", "constants"],
    "test": ["test", "spec", "fixture", "mock"],
    "util": ["util", "helper", "common", "shared", "lib"],
}


def _classify_layer(file_path: str) -> str | None:
    """Classify a file into an architectural layer based on path components.

    Scans directory and filename components against known layer keywords.
    Returns the layer name or None if no match.
    """
    parts = file_path.lower().replace("\\", "/").split("/")
    for layer, keywords in LAYER_PATTERNS.items():
        if any(kw in part for part in parts for kw in keywords):
            return layer
    return None


def _extract_identifiers(text: str) -> list[str]:
    """Extract likely symbol names from issue/task text."""
    backtick = re.findall(r"`([A-Za-z_]\w*(?:\.\w+)*)`", text)
    dotted = re.findall(r"\b([A-Za-z_]\w+\.[A-Za-z_]\w+(?:\.\w+)*)\b", text)
    func_calls = re.findall(r"\b([A-Za-z_]\w+)\s*\(", text)
    seen: set[str] = set()
    result: list[str] = []
    for name in backtick + dotted + func_calls:
        if name not in seen and len(name) > 2:
            seen.add(name)
            result.append(name)
    return result


async def handle_orient(
    task: str | None,
    file_path: str | None,
    store: GraphStore,
    graph: ImportGraph,
    novelty: NoveltyFilter,
) -> str:
    if not task and not file_path:
        return ""

    findings: list[Finding] = []

    if file_path:
        matched = store._match_file_path(file_path)
        syms_result = store.get_symbols_in_file(matched)
        if not isinstance(syms_result, Err) and syms_result.value:
            exported = [s for s in syms_result.value if s.is_exported]
            layer = _classify_layer(matched)
            layer_tag = f" [{layer}]" if layer else ""
            findings.append(Finding(
                kind=FindingKind.FILE_RELEVANCE,
                severity=Severity.NOTE,
                confidence=1.0,
                location=Location(file=matched),
                message=f"{len(syms_result.value)} symbols ({len(exported)} exported){layer_tag}",
                agent_action=AgentAction.READ,
                why_now=WhyNow.ALWAYS,
            ))

        importers_result = store.get_importers_of_file(matched, min_confidence=MIN_CONFIDENCE)
        if not isinstance(importers_result, Err) and importers_result.value:
            importers = importers_result.value[:5]
            findings.append(Finding(
                kind=FindingKind.CALLER_EXPECTATION,
                severity=Severity.NOTE,
                confidence=1.0,
                location=Location(file=matched),
                message=f"imported by: {', '.join(importers[:3])}{'...' if len(importers) > 3 else ''}",
                agent_action=AgentAction.READ,
                why_now=WhyNow.ALWAYS,
            ))

    if task:
        identifiers = _extract_identifiers(task)
        for ident in identifiers[:MAX_LOCALIZED]:
            parts = ident.split(".")
            name = parts[-1]
            syms_result = store.find_symbol_by_name(name)
            if isinstance(syms_result, Err) or not syms_result.value:
                continue
            for sym in syms_result.value[:1]:
                refs_result = store.get_refs_for_symbol(sym.id, min_confidence=MIN_CONFIDENCE)
                has_high_conf = not isinstance(refs_result, Err) and len(refs_result.value) > 0
                if not has_high_conf and sym.usage_count == 0:
                    continue
                sym_layer = _classify_layer(sym.file_path)
                sym_layer_tag = f" [{sym_layer}]" if sym_layer else ""
                # Compact 1-line contract summary (raises / preserve / returns)
                # for the localized symbol. Correct-or-quiet: "" when no signal.
                sym_contract = contract_line_for(store, sym.file_path, sym.name)
                contract_tag = f" — {sym_contract}" if sym_contract else ""
                findings.append(Finding(
                    kind=FindingKind.FILE_RELEVANCE,
                    severity=Severity.WARNING,
                    confidence=0.90,
                    location=Location(file=sym.file_path, line=sym.line_number, symbol=sym.name),
                    message=f"FIX HERE: {sym.name}(){sym_layer_tag}{contract_tag}",
                    agent_action=AgentAction.READ,
                    why_now=WhyNow.ALWAYS,
                ))
                if not isinstance(refs_result, Err):
                    for ref in refs_result.value[:MAX_CALLERS_PER_SYMBOL]:
                        if not ref.referenced_in_file:
                            continue
                        findings.append(Finding(
                            kind=FindingKind.CALLER_EXPECTATION,
                            severity=Severity.WARNING,
                            confidence=0.75,
                            location=Location(file=sym.file_path, line=sym.line_number, symbol=sym.name),
                            message=f"caller at {ref.referenced_in_file}:{ref.referenced_at_line or '?'}",
                            evidence_locations=[Location(file=ref.referenced_in_file, line=ref.referenced_at_line)],
                            agent_action=AgentAction.VERIFY,
                            why_now=WhyNow.ALWAYS,
                        ))

        hotspots_result = store.get_hotspots(MAX_HOTSPOTS, min_confidence=MIN_CONFIDENCE)
        if not isinstance(hotspots_result, Err):
            localized_names = {f.location.symbol for f in findings if f.location.symbol}
            for hs in hotspots_result.value:
                if hs.name in localized_names:
                    findings.append(Finding(
                        kind=FindingKind.FILE_RELEVANCE,
                        severity=Severity.NOTE,
                        confidence=0.95,
                        location=Location(file=hs.file_path, line=hs.line_number, symbol=hs.name),
                        message=f"HOTSPOT: {hs.name} has {hs.usage_count} verified callers",
                        agent_action=AgentAction.READ,
                        why_now=WhyNow.ALWAYS,
                    ))

    pruned = prune_findings(novelty.filter(findings), confidence_floor=MIN_CONFIDENCE)
    if not pruned:
        if file_path:
            return f'<gt-evidence surface="orient">\n[SKIP] {file_path} not indexed\n</gt-evidence>'
        return ""
    text = format_findings(pruned, "orient")
    return enforce_budget(text, TOKEN_BUDGET)
