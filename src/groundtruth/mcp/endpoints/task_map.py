"""groundtruth_task_map — Pre-task localization + repo shape.

Surface 1 of 3 in the Decision Interface vNext.

When: Once at task start, before any file reads or edits.
What: Deterministic localization and structural constraints for
      symbols mentioned in the issue text.
"""

from __future__ import annotations

import os
import re
from typing import Any

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.schema.finding import (
    AgentAction,
    Finding,
    FindingKind,
    Location,
    Severity,
    WhyNow,
    format_findings,
)
from groundtruth.schema.novelty import NoveltyFilter
from groundtruth.schema.pruning import prune_findings
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Ok

log = get_logger("endpoints.task_map")

_MAX_TARGETS = 5
_MAX_CALLERS_PER_TARGET = 3


def _extract_identifiers(issue_text: str) -> list[str]:
    """Extract plausible symbol names from issue text."""
    tokens: list[str] = []
    for match in re.finditer(r"`([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)`", issue_text):
        tokens.append(match.group(1))
    for match in re.finditer(
        r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b", issue_text
    ):
        candidate = match.group(1)
        if candidate not in tokens:
            tokens.append(candidate)
    for match in re.finditer(r"\b([a-z_]\w+)\(\)", issue_text):
        candidate = match.group(1)
        if candidate not in tokens:
            tokens.append(candidate)
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _resolve_targets(
    identifiers: list[str],
    store: SymbolStore,
) -> list[dict[str, Any]]:
    """Resolve identifiers to symbol records via find_symbol_by_name."""
    targets: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for ident in identifiers:
        parts = ident.rsplit(".", 1)
        name = parts[-1]
        result = store.find_symbol_by_name(name)
        if not isinstance(result, Ok):
            continue
        for sym in result.value[:5]:
            key = f"{sym.file_path}:{sym.name}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            targets.append(
                {
                    "name": sym.name,
                    "file": sym.file_path,
                    "line": sym.line_number,
                    "kind": sym.kind,
                    "signature": getattr(sym, "signature", None),
                }
            )
            if len(targets) >= _MAX_TARGETS:
                return targets
    return targets


def _build_findings(
    targets: list[dict[str, Any]],
    graph: ImportGraph,
    root_path: str,
) -> list[Finding]:
    """Build findings from resolved targets."""
    findings: list[Finding] = []

    for target in targets:
        findings.append(
            Finding(
                kind=FindingKind.FILE_RELEVANCE,
                severity=Severity.NOTE,
                confidence=0.9,
                location=Location(
                    file=target["file"],
                    line=target["line"],
                    symbol=target["name"],
                ),
                message=f"FIX HERE: {target['name']}()",
                why_now=WhyNow.FILE_OPENED,
                agent_action=AgentAction.READ,
                rule_id="GT-LOC-FILE",
            )
        )

        callers_result = graph.find_callers(target["name"])
        if not isinstance(callers_result, Ok):
            continue
        cross_file = [
            c for c in callers_result.value
            if c.file_path != target["file"]
        ][:_MAX_CALLERS_PER_TARGET]
        for caller in cross_file:
            usage_hint = ""
            if caller.file_path and caller.line:
                try:
                    full = os.path.join(root_path, caller.file_path)
                    with open(full, encoding="utf-8", errors="replace") as f:
                        for i, ln in enumerate(f, 1):
                            if i == caller.line:
                                usage_hint = ln.strip()[:80]
                                break
                except OSError:
                    pass
            findings.append(
                Finding(
                    kind=FindingKind.CALLER_EXPECTATION,
                    severity=Severity.WARNING,
                    confidence=0.7,
                    location=Location(
                        file=target["file"],
                        line=target["line"],
                        symbol=target["name"],
                    ),
                    evidence_locations=[
                        Location(file=caller.file_path, line=caller.line)
                    ],
                    message=f"caller at {caller.file_path}:{caller.line}"
                    + (f" — {usage_hint}" if usage_hint else ""),
                    why_now=WhyNow.FILE_OPENED,
                    agent_action=AgentAction.VERIFY,
                    rule_id="GT-LOC-CALLER",
                )
            )

    return findings


async def handle_task_map(
    issue_text: str,
    store: SymbolStore,
    graph: ImportGraph,
    root_path: str,
    novelty_filter: NoveltyFilter | None = None,
    *,
    entry_files: list[str] | None = None,
) -> dict[str, Any]:
    """Pre-task localization and repo shape."""
    identifiers = _extract_identifiers(issue_text)
    targets = _resolve_targets(identifiers, store)

    if entry_files:
        for ef in entry_files:
            if not any(t["file"] == ef for t in targets):
                targets.append(
                    {"name": os.path.basename(ef), "file": ef, "line": None, "kind": "file", "signature": None}
                )

    findings = _build_findings(targets, graph, root_path)

    if novelty_filter:
        findings = novelty_filter.filter(findings)
    findings = prune_findings(findings, confidence_floor=0.5)

    text = format_findings(findings, "task_map")

    return {
        "findings": [f.model_dump() for f in findings],
        "text": text,
        "targets": targets,
        "identifiers": identifiers,
    }
