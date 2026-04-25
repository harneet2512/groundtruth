"""groundtruth_review_patch — Pre-submit deterministic diff review.

Surface 3 of 3 in the Decision Interface vNext.

When: Once before submitting the patch.
What: Run ALL engines (obligations, contradictions, change analysis,
      call-site voting, argument affinity, guard consistency) on the
      full diff. Emit binding awareness footer for FIX_REQUIRED findings.
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
    semantic_evidence_to_finding,
)
from groundtruth.schema.finding import Finding, format_findings
from groundtruth.schema.novelty import NoveltyFilter
from groundtruth.schema.pruning import prune_findings
from groundtruth.utils.logger import get_logger

log = get_logger("endpoints.review_patch")

_SUPPORTED_EXTENSIONS = frozenset(
    {
        ".py", ".go", ".js", ".jsx", ".ts", ".tsx", ".rs", ".java",
        ".kt", ".kts", ".scala", ".cs", ".php", ".swift", ".c", ".h",
        ".cpp", ".cc", ".cxx", ".hpp", ".rb", ".ex", ".exs", ".lua",
        ".ml", ".groovy", ".mjs", ".cjs",
    }
)


def _get_full_diff(root_path: str) -> str:
    """Get full git diff."""
    try:
        result = subprocess.run(
            ["git", "diff"],
            capture_output=True,
            text=True,
            cwd=root_path,
            timeout=15,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _get_modified_files(root_path: str) -> list[str]:
    """Get modified source files from git diff."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True,
            text=True,
            cwd=root_path,
            timeout=10,
        )
        return [
            line.strip()
            for line in result.stdout.strip().split("\n")
            if line.strip() and os.path.splitext(line.strip())[1].lower() in _SUPPORTED_EXTENSIONS
        ]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _run_all_engines(
    root_path: str,
    diff_text: str,
    modified_files: list[str],
    store: SymbolStore,
    graph: ImportGraph,
) -> list[Finding]:
    """Run all deterministic engines and collect findings."""
    findings: list[Finding] = []

    # 1. Obligations
    try:
        from groundtruth.validators.obligations import ObligationEngine

        engine = ObligationEngine(store, graph)
        for ob in engine.infer_from_patch(diff_text):
            findings.append(obligation_to_finding(ob))
    except (ImportError, Exception) as exc:
        log.debug("obligation engine: %s", exc)

    # 2. Contradictions
    try:
        from groundtruth.validators.contradictions import ContradictionDetector

        detector = ContradictionDetector(store)
        for fp in modified_files[:5]:
            try:
                full = os.path.join(root_path, fp)
                with open(full, encoding="utf-8", errors="replace") as f:
                    source = f.read()
                for c in detector.check_file(fp, source):
                    findings.append(contradiction_to_finding(c))
            except (OSError, Exception):
                pass
    except (ImportError, Exception) as exc:
        log.debug("contradiction detector: %s", exc)

    # 3. Change analysis
    try:
        from groundtruth.evidence.change import ChangeAnalyzer

        analyzer = ChangeAnalyzer()
        for ce in analyzer.analyze(root_path, diff_text):
            findings.append(change_evidence_to_finding(ce))
    except (ImportError, Exception) as exc:
        log.debug("change analyzer: %s", exc)

    # 4. Semantic signals
    try:
        from groundtruth.evidence.semantic.call_site_voting import CallSiteVoter

        for se in CallSiteVoter().analyze(root_path, diff_text):
            f = semantic_evidence_to_finding(se)
            if f is not None:
                findings.append(f)
    except (ImportError, Exception) as exc:
        log.debug("call-site voting: %s", exc)

    try:
        from groundtruth.evidence.semantic.argument_affinity import ArgumentAffinityChecker

        for se in ArgumentAffinityChecker().analyze(root_path, diff_text):
            f = semantic_evidence_to_finding(se)
            if f is not None:
                findings.append(f)
    except (ImportError, Exception) as exc:
        log.debug("argument affinity: %s", exc)

    try:
        from groundtruth.evidence.semantic.guard_consistency import GuardConsistencyChecker

        for se in GuardConsistencyChecker().analyze(root_path, diff_text):
            f = semantic_evidence_to_finding(se)
            if f is not None:
                findings.append(f)
    except (ImportError, Exception) as exc:
        log.debug("guard consistency: %s", exc)

    return findings


async def handle_review_patch(
    store: SymbolStore,
    graph: ImportGraph,
    root_path: str,
    novelty_filter: NoveltyFilter | None = None,
    *,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Pre-submit deterministic diff review.

    Runs ALL engines. Includes binding awareness footer for
    FIX_REQUIRED findings.
    """
    if file_path:
        try:
            result = subprocess.run(
                ["git", "diff", "--", file_path],
                capture_output=True,
                text=True,
                cwd=root_path,
                timeout=15,
            )
            diff_text = result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            diff_text = ""
        modified_files = [file_path] if diff_text else []
    else:
        diff_text = _get_full_diff(root_path)
        modified_files = _get_modified_files(root_path)

    if not diff_text:
        return {
            "findings": [],
            "text": "",
            "modified_files": [],
        }

    findings = _run_all_engines(root_path, diff_text, modified_files, store, graph)

    if novelty_filter:
        findings = novelty_filter.filter(findings)
    findings = prune_findings(findings)

    text = format_findings(findings, "review_patch", include_binding=True)

    return {
        "findings": [f.model_dump() for f in findings],
        "text": text,
        "modified_files": modified_files,
    }
