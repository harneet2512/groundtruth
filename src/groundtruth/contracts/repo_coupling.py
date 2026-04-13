"""Root-aware coupling contract builders.

These builders translate repository-level coupling edges into runtime
contracts that can participate in confidence-gated verification.
"""

from __future__ import annotations

from collections import defaultdict

from groundtruth.repo_intel.coupling_rules import CouplingExtractor
from groundtruth.substrate.types import ContractRecord, tier_from_confidence


def build_repo_coupling_contracts(
    reader,
    root: str,
    modified_files: list[str],
) -> list[ContractRecord]:  # noqa: ANN001
    """Build config/doc coupling contracts for modified source files."""
    extractor = CouplingExtractor(reader)
    edges = extractor.extract(root)
    by_target_and_type: dict[tuple[str, str], list] = defaultdict(list)

    modified = set(modified_files)
    for edge in edges:
        target = _normalize(edge.target_file)
        if target not in modified:
            continue
        if edge.coupling_type not in {"config", "doc"}:
            continue
        by_target_and_type[(target, edge.coupling_type)].append(edge)

    contracts: list[ContractRecord] = []
    for (target_file, coupling_type), grouped_edges in by_target_and_type.items():
        support_sources = tuple(_normalize(edge.source_file) + ":0" for edge in grouped_edges[:5])
        support_count = len(grouped_edges)
        confidence = max(edge.confidence for edge in grouped_edges)
        tier = tier_from_confidence(confidence, support_count)
        contracts.append(
            ContractRecord(
                contract_type=f"{coupling_type}_coupling",
                scope_kind="file",
                scope_ref=target_file,
                predicate=_predicate_for(coupling_type, target_file, grouped_edges),
                normalized_form=_normalized_form(coupling_type, target_file, grouped_edges[0].source_file),
                support_sources=support_sources,
                support_count=support_count,
                confidence=confidence,
                tier=tier,
            )
        )

    return contracts


def _predicate_for(coupling_type: str, target_file: str, edges: list) -> str:
    source_name = _normalize(edges[0].source_file)
    if coupling_type == "config":
        return f"Changes to {target_file} must preserve config coupling with {source_name}"
    return f"Changes to {target_file} must preserve documented coupling with {source_name}"


def _normalized_form(coupling_type: str, target_file: str, source_file: str) -> str:
    return f"{coupling_type}_coupling:preserve_file:{_normalize(target_file)}:{_normalize(source_file)}"


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")
