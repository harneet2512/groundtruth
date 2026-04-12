"""Adapter: gt_intel.py calls into substrate via this thin shim.

gt_intel.py continues to work standalone (raw sqlite3), but when the
substrate is available, it delegates to SubstrateService for evidence
production. This enables progressive migration without breaking the
existing pipeline.

Usage in gt_intel.py:
    from groundtruth.substrate.adapter import try_substrate_evidence
    result = try_substrate_evidence(db_path, target_name, target_file, root)
    if result is not None:
        return result  # Use substrate output
    # ... fallback to inline logic
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def try_substrate_evidence(
    db_path: str,
    target_name: str,
    target_file: str,
    root: str,
    token_budget: int = 450,
) -> list[dict[str, Any]] | None:
    """Try to compute evidence via substrate. Returns None if unavailable.

    This function is the bridge between gt_intel.py and the new substrate.
    It returns evidence in the same dict format that gt_intel's internal
    code uses, so the rest of gt_intel's rendering pipeline works unchanged.

    Returns None if:
    - substrate modules are not importable (bare container without groundtruth)
    - no producers are registered yet (early phases)
    - GraphStore fails to initialize
    - any unexpected error occurs
    """
    try:
        from groundtruth.index.graph_store import GraphStore
        from groundtruth.substrate.graph_reader_impl import GraphStoreReader
        from groundtruth.substrate.service import SubstrateService

        # Try to import extractors — if none exist yet, bail out
        extractors = _get_registered_extractors()
        producers = _get_registered_producers()

        if not extractors and not producers:
            return None  # No semantic logic registered yet — use fallback

        # Initialize graph store
        store = GraphStore(db_path)
        init_result = store.initialize()
        if hasattr(init_result, "is_err") and init_result.is_err():
            return None

        reader = GraphStoreReader(store)
        service = SubstrateService(reader)

        # Register available extractors and producers
        for ext in extractors:
            service.register_extractor(ext)
        for prod in producers:
            service.register_producer(prod)

        # Find target node
        node = reader.get_node_by_name(target_name, target_file)
        if node is None:
            return None

        node_id = node["id"]

        # Get contracts and evidence
        contracts, evidence = service.get_guidance(
            node_id, root, token_budget
        )

        # Convert to gt_intel-compatible format (EvidenceNode has exactly 7 fields:
        # family, score, name, file, line, source_code, summary)
        # Do NOT include confidence/tier — they cause TypeError on EvidenceNode(**r)
        results = []
        for item in evidence:
            results.append({
                "family": item.family,
                "score": item.score,
                "name": item.name,
                "file": item.file,
                "line": item.line,
                "source_code": item.source_code,
                "summary": item.summary,
            })

        # Inject contract-derived evidence as OBLIGATION family
        for contract in contracts:
            results.append({
                "family": "OBLIGATION",
                "score": 3 if contract.tier == "verified" else 2,
                "name": contract.scope_ref,
                "file": "",
                "line": 0,
                "source_code": contract.normalized_form,
                "summary": f"MUST PRESERVE: {contract.predicate}",
            })

        logger.info(
            "Substrate path produced %d evidence items + %d contracts",
            len(evidence), len(contracts),
        )
        return results if results else None

    except ImportError:
        return None  # Substrate not installed
    except Exception as exc:
        logger.debug("Substrate adapter failed: %s", exc)
        return None  # Any failure → graceful fallback


def _get_registered_extractors() -> list:
    """Discover and instantiate available contract extractors.

    Returns empty list if no extractors are available yet.
    This is the registry point — as extractors are added in Phase 1,
    they get imported here.
    """
    extractors = []
    try:
        from groundtruth.contracts.extractors.exception_extractor import (
            ExceptionExtractor,
        )
        extractors.append(ExceptionExtractor())
    except ImportError:
        pass

    try:
        from groundtruth.contracts.extractors.output_extractor import (
            OutputExtractor,
        )
        extractors.append(OutputExtractor())
    except ImportError:
        pass

    try:
        from groundtruth.contracts.extractors.roundtrip_extractor import (
            RoundtripExtractor,
        )
        extractors.append(RoundtripExtractor())
    except ImportError:
        pass

    try:
        from groundtruth.contracts.extractors.obligation_extractor import (
            ObligationExtractor,
        )
        extractors.append(ObligationExtractor())
    except ImportError:
        pass

    try:
        from groundtruth.contracts.extractors.type_shape_extractor import (
            TypeShapeExtractor,
        )
        extractors.append(TypeShapeExtractor())
    except ImportError:
        pass

    try:
        from groundtruth.contracts.extractors.negative_extractor import (
            NegativeExtractor,
        )
        extractors.append(NegativeExtractor())
    except ImportError:
        pass

    return extractors


def _get_registered_producers() -> list:
    """Discover and instantiate available evidence producers.

    Returns available producers as evidence families migrate from
    gt_intel.py into the substrate.
    """
    producers = []
    try:
        from groundtruth.evidence_producers import SiblingProducer

        producers.append(SiblingProducer())
    except ImportError:
        pass

    return producers
