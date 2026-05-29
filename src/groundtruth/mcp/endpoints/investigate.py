"""groundtruth_investigate — Symbol deep-dive with confidence gating.

Absorbs: trace, context, explain, impact, patterns.
Agent asks: "Tell me about this symbol."
Token budget: 400. Confidence floor: 0.7. Silent when nothing passes gate.
"""

from __future__ import annotations

from groundtruth.index.graph import ImportGraph
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
MAX_CALLERS = 5
MAX_CALLEES = 3


async def handle_investigate(
    symbol: str,
    file_path: str | None,
    store: GraphStore,
    graph: ImportGraph,
    novelty: NoveltyFilter,
) -> str:
    syms_result = store.find_symbol_by_name(symbol)
    if isinstance(syms_result, Err) or not syms_result.value:
        return f'<gt-evidence surface="investigate">\n[SKIP] Symbol \'{symbol}\' not found in index\n</gt-evidence>'

    syms = syms_result.value
    if file_path:
        matched = [s for s in syms if s.file_path.endswith(file_path) or file_path.endswith(s.file_path)]
        if matched:
            syms = matched

    target = syms[0]
    findings: list[Finding] = []

    findings.append(Finding(
        kind=FindingKind.FILE_RELEVANCE,
        severity=Severity.NOTE,
        confidence=1.0,
        location=Location(file=target.file_path, line=target.line_number, symbol=target.name),
        message=f"{target.name}() defined — {target.signature or target.kind}",
        agent_action=AgentAction.READ,
        why_now=WhyNow.ALWAYS,
    ))

    refs_result = store.get_refs_for_symbol(target.id, min_confidence=MIN_CONFIDENCE)
    caller_files: set[str] = set()
    if not isinstance(refs_result, Err):
        for ref in refs_result.value[:MAX_CALLERS]:
            if not ref.referenced_in_file:
                continue
            caller_files.add(ref.referenced_in_file)
            findings.append(Finding(
                kind=FindingKind.CALLER_CONTRACT,
                severity=Severity.WARNING,
                confidence=0.9,
                location=Location(file=target.file_path, line=target.line_number, symbol=target.name),
                message=f"caller at {ref.referenced_in_file}:{ref.referenced_at_line or '?'}",
                evidence_locations=[Location(file=ref.referenced_in_file, line=ref.referenced_at_line)],
                agent_action=AgentAction.VERIFY,
                why_now=WhyNow.ALWAYS,
            ))

        total_callers = len(refs_result.value)
        total_files = len({r.referenced_in_file for r in refs_result.value if r.referenced_in_file})
        if total_callers > 0:
            findings.append(Finding(
                kind=FindingKind.CALLER_EXPECTATION,
                severity=Severity.WARNING,
                confidence=0.75,
                location=Location(file=target.file_path, line=target.line_number, symbol=target.name),
                message=f"{total_callers} callers in {total_files} files, impact: {total_files} files",
                agent_action=AgentAction.VERIFY,
                why_now=WhyNow.ALWAYS,
            ))

    pruned = prune_findings(novelty.filter(findings), confidence_floor=MIN_CONFIDENCE)
    if not pruned:
        return ""
    text = format_findings(pruned, "investigate")

    # Append the deterministic contract (raises / guards / return shape) for the
    # target symbol. Always-available, node-local, correct-or-quiet: empty when
    # nothing was extracted or the db has no properties table. Append BEFORE
    # enforce_budget so the contract block is inside the token budget (not
    # bypassing it) and the budget pass owns the final structure.
    contract = contract_block_for(store, target.file_path, target.name)
    if contract:
        text = f"{text}\n{contract}"
    return enforce_budget(text, TOKEN_BUDGET)
