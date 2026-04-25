"""groundtruth_event_brief — Post-edit, only new high-value facts.

Surface 2 of 3 in the Decision Interface vNext.

When: After each file edit.
What: Run change analysis + contradiction check on the modified file.
      Emit only novel findings. Silent when nothing to say.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.schema.adapters import (
    change_evidence_to_finding,
    contradiction_to_finding,
    obligation_to_finding,
)
from groundtruth.schema.finding import Finding, format_findings
from groundtruth.schema.novelty import NoveltyFilter
from groundtruth.schema.pruning import prune_findings
from groundtruth.utils.logger import get_logger

log = get_logger("endpoints.event_brief")


def _get_file_diff(root_path: str, file_path: str) -> str:
    """Get git diff for a specific file."""
    try:
        result = subprocess.run(
            ["git", "diff", "--", file_path],
            capture_output=True,
            text=True,
            cwd=root_path,
            timeout=10,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _run_change_analysis(
    root_path: str,
    file_path: str,
    diff_text: str,
) -> list[Finding]:
    """Run change evidence detection and convert to findings."""
    findings: list[Finding] = []
    try:
        from groundtruth.evidence.change import ChangeAnalyzer

        analyzer = ChangeAnalyzer()
        evidence_items = analyzer.analyze(root_path, diff_text)
        for ce in evidence_items:
            if ce.file_path == file_path or not file_path:
                findings.append(change_evidence_to_finding(ce))
    except (ImportError, Exception) as exc:
        log.debug("change analysis unavailable: %s", exc)
    return findings


def _run_contradiction_check(
    root_path: str,
    file_path: str,
    store: SymbolStore,
) -> list[Finding]:
    """Run contradiction detection and convert to findings."""
    findings: list[Finding] = []
    try:
        from groundtruth.validators.contradictions import ContradictionDetector

        detector = ContradictionDetector(store)
        source_code = ""
        try:
            full = os.path.join(root_path, file_path)
            with open(full, encoding="utf-8", errors="replace") as f:
                source_code = f.read()
        except OSError:
            return findings
        for c in detector.check_file(file_path, source_code):
            findings.append(contradiction_to_finding(c))
    except (ImportError, AttributeError, Exception) as exc:
        log.debug("contradiction check unavailable: %s", exc)
    return findings


def _run_obligation_check(
    diff_text: str,
    store: SymbolStore,
    graph: ImportGraph,
) -> list[Finding]:
    """Run obligation engine and convert to findings."""
    findings: list[Finding] = []
    try:
        from groundtruth.validators.obligations import ObligationEngine

        engine = ObligationEngine(store, graph)
        obligations = engine.infer_from_patch(diff_text)
        for ob in obligations:
            findings.append(obligation_to_finding(ob))
    except (ImportError, AttributeError, Exception) as exc:
        log.debug("obligation engine unavailable: %s", exc)
    return findings


async def handle_event_brief(
    file_path: str,
    store: SymbolStore,
    graph: ImportGraph,
    root_path: str,
    novelty_filter: NoveltyFilter | None = None,
) -> dict[str, Any]:
    """Post-edit: only new deterministic findings for the just-modified file.

    Returns empty text when there are no novel findings (silent when
    nothing to say).
    """
    diff_text = _get_file_diff(root_path, file_path)

    findings: list[Finding] = []
    findings.extend(_run_change_analysis(root_path, file_path, diff_text))
    findings.extend(_run_contradiction_check(root_path, file_path, store))
    if diff_text:
        findings.extend(_run_obligation_check(diff_text, store, graph))

    if novelty_filter:
        findings = novelty_filter.filter(findings)
    findings = prune_findings(findings)

    text = format_findings(findings, "event_brief")

    return {
        "findings": [f.model_dump() for f in findings],
        "text": text,
        "file_path": file_path,
    }
