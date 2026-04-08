"""Structural evidence -- thin wrapper around existing obligation/contradiction/convention modules.

Converts findings from ObligationEngine, ContradictionDetector, and ConventionChecker
into a common EvidenceItem format for unified abstention and formatting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class StructuralEvidence:
    """A structural finding from existing validators."""

    kind: str  # obligation | contradiction | convention
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "structural"


def run_obligations(store, graph, diff_text: str) -> list[StructuralEvidence]:
    """Run ObligationEngine and convert to evidence items."""
    try:
        from groundtruth.validators.obligations import ObligationEngine

        engine = ObligationEngine(store, graph)
        obligations = engine.infer_from_patch(diff_text)
        return [
            StructuralEvidence(
                kind="obligation",
                file_path=ob.target_file,
                line=ob.target_line or 0,
                message=f"{ob.target} -- {ob.reason}",
                confidence=ob.confidence,
            )
            for ob in obligations
        ]
    except Exception:
        return []


def run_contradictions(store, root: str, modified_files: list[str]) -> list[StructuralEvidence]:
    """Run ContradictionDetector and convert to evidence items."""
    try:
        from groundtruth.validators.contradictions import ContradictionDetector

        detector = ContradictionDetector(store)
        results = []
        for fpath in modified_files[:5]:
            try:
                with open(os.path.join(root, fpath), "r", errors="replace") as f:
                    source = f.read()
            except OSError:
                continue
            for c in detector.check_file(fpath, source):
                results.append(
                    StructuralEvidence(
                        kind="contradiction",
                        file_path=c.file_path,
                        line=c.line or 0,
                        message=c.message,
                        confidence=c.confidence,
                    )
                )
        return results
    except Exception:
        return []


def run_conventions(root: str, modified_files: list[str]) -> list[StructuralEvidence]:
    """Run ConventionChecker and convert to evidence items."""
    try:
        from groundtruth.analysis.conventions import detect_all

        results = []
        for fpath in modified_files[:5]:
            try:
                with open(os.path.join(root, fpath), "r", errors="replace") as f:
                    source = f.read()
            except OSError:
                continue
            for conv in detect_all(source, scope=fpath):
                if conv.frequency < 1.0 and conv.confidence >= 0.6:
                    results.append(
                        StructuralEvidence(
                            kind="convention",
                            file_path=fpath,
                            line=0,
                            message=conv.pattern,
                            confidence=conv.confidence,
                        )
                    )
        return results
    except Exception:
        return []
