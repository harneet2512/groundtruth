"""groundtruth_check (consolidated) — Diff-aware validation with confidence gating.

Absorbs: validate, event_brief, review_patch.
Agent asks: "Is my edit correct?"
Token budget: 400. Confidence floor: 0.7. Silent when nothing passes gate.
"""

from __future__ import annotations

from groundtruth.index.graph_store import GraphStore
from groundtruth.mcp.endpoints._contract import contract_block_for
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


async def handle_check(
    file_path: str | None,
    proposed_code: str | None,
    store: GraphStore,
    novelty: NoveltyFilter,
) -> str:
    """Validate agent's edit against the call graph.

    Modes:
    - file_path + proposed_code: pre-write validation
    - file_path only: post-edit check on that file
    - neither: full diff review (pre-submit)
    """
    if not file_path and not proposed_code:
        return ""

    findings: list[Finding] = []
    contract_syms: list[tuple[str, str]] = []

    if file_path:
        matched = store._match_file_path(file_path)
        syms_result = store.get_symbols_in_file(matched)
        if isinstance(syms_result, Err) or not syms_result.value:
            return ""

        for sym in syms_result.value:
            if not sym.is_exported:
                continue
            refs_result = store.get_refs_for_symbol(sym.id, min_confidence=MIN_CONFIDENCE)
            if isinstance(refs_result, Err):
                continue
            callers = refs_result.value
            if not callers:
                continue
            contract_syms.append((matched, sym.name))

            caller_files = {r.referenced_in_file for r in callers if r.referenced_in_file}
            if len(callers) >= 3:
                findings.append(Finding(
                    kind=FindingKind.CALLER_CONTRACT,
                    severity=Severity.WARNING,
                    confidence=0.80,
                    location=Location(file=matched, line=sym.line_number, symbol=sym.name),
                    message=f"{sym.name}() has {len(callers)} callers in {len(caller_files)} files — verify signature compatibility",
                    agent_action=AgentAction.VERIFY,
                    why_now=WhyNow.FILE_CHANGED,
                ))

            if sym.return_type:
                findings.append(Finding(
                    kind=FindingKind.CALLER_EXPECTATION,
                    severity=Severity.WARNING,
                    confidence=0.75,
                    location=Location(file=matched, line=sym.line_number, symbol=sym.name),
                    message=f"callers expect return type: {sym.return_type}",
                    agent_action=AgentAction.VERIFY,
                    why_now=WhyNow.FILE_CHANGED,
                ))

    pruned = prune_findings(novelty.filter(findings), confidence_floor=MIN_CONFIDENCE)
    if not pruned:
        return ""
    text = format_findings(pruned, "check", include_binding=True)

    # Augment the thin "callers expect return type" line with the full
    # deterministic contract (raises + guards + return shape) for each changed
    # symbol that has callers. Correct-or-quiet: empty blocks are skipped.
    # Append BEFORE enforce_budget so the contract blocks are inside the token
    # budget (not bypassing it) and the budget pass owns the final structure.
    for cfile, cname in contract_syms[:3]:
        block = contract_block_for(store, cfile, cname)
        if block:
            text = f"{text}\n{block}"
    return enforce_budget(text, TOKEN_BUDGET)
