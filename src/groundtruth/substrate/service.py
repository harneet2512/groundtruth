"""SubstrateService — orchestrates contract extraction and evidence production.

This is the single entry point for all semantic logic. Benchmark adapters
and hooks call into this service rather than implementing extraction inline.
"""

from __future__ import annotations

from groundtruth.substrate.protocols import (
    ContractExtractor,
    EvidenceProducer,
    GraphReader,
)
from groundtruth.substrate.types import ContractRecord, EvidenceItem


class SubstrateService:
    """Composes extractors and producers behind a single typed interface.

    Usage:
        reader = GraphStoreReader(store)
        service = SubstrateService(reader)
        service.register_extractor(ExceptionExtractor())
        service.register_producer(CallerProducer())

        contracts = service.extract_contracts(node_id)
        evidence = service.compute_evidence(node_id, root="/path/to/repo")
        selected = service.rank_and_select(evidence, token_budget=450)
    """

    def __init__(self, reader: GraphReader) -> None:
        self._reader = reader
        self._extractors: list[ContractExtractor] = []
        self._producers: list[EvidenceProducer] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_extractor(self, extractor: ContractExtractor) -> None:
        """Register a contract extractor."""
        self._extractors.append(extractor)

    def register_producer(self, producer: EvidenceProducer) -> None:
        """Register an evidence producer."""
        self._producers.append(producer)

    # ------------------------------------------------------------------
    # Contract extraction
    # ------------------------------------------------------------------

    def extract_contracts(self, node_id: int) -> list[ContractRecord]:
        """Run all registered extractors. Suppress 'possible' tier results.

        Returns only 'verified' and 'likely' contracts — the engine
        handles per-tier rendering downstream.
        """
        results: list[ContractRecord] = []
        for extractor in self._extractors:
            try:
                contracts = extractor.extract(self._reader, node_id)
                results.extend(c for c in contracts if c.tier != "possible")
            except Exception:
                # Extractor failure must not crash the pipeline
                continue
        return results

    # ------------------------------------------------------------------
    # Evidence production
    # ------------------------------------------------------------------

    def compute_evidence(
        self, node_id: int, root: str
    ) -> list[EvidenceItem]:
        """Run all registered producers. Returns unsorted candidates.

        Each producer generates items for one evidence family.
        The service collects all items without ranking — call
        rank_and_select() to apply the token-budgeted knapsack.
        """
        results: list[EvidenceItem] = []
        for producer in self._producers:
            try:
                items = producer.produce(self._reader, node_id, root)
                results.extend(items)
            except Exception:
                continue
        return results

    def rank_and_select(
        self,
        candidates: list[EvidenceItem],
        token_budget: int = 450,
        per_family_caps: dict[str, int] | None = None,
    ) -> list[EvidenceItem]:
        """Token-budgeted knapsack selection.

        Ported from gt_intel.py's rank_and_select logic:
        - Per-family caps limit how many items from each family
        - Items sorted by (-score, structural_priority, family_priority)
        - Items accumulated until token_budget is exhausted

        Args:
            candidates: Unranked evidence items from compute_evidence().
            token_budget: Max estimated tokens for the output.
            per_family_caps: Override per-family max items.
                Defaults: NEGATIVE/OBLIGATION/CRITIQUE=2, TEST/CALLER=3, others=1

        Returns:
            Selected items in ranked order.
        """
        if not candidates:
            return []

        caps = per_family_caps or {
            "NEGATIVE": 2,
            "OBLIGATION": 2,
            "CRITIQUE": 2,
            "TEST": 3,
            "CALLER": 3,
            "SIBLING": 1,
            "IMPACT": 1,
            "TYPE": 1,
            "PRECEDENT": 1,
            "IMPORT": 1,
        }

        # Family priority (lower = more important)
        family_priority: dict[str, int] = {
            "NEGATIVE": 0,
            "OBLIGATION": 1,
            "CRITIQUE": 2,
            "TEST": 3,
            "CALLER": 4,
            "SIBLING": 5,
            "IMPACT": 6,
            "TYPE": 7,
            "IMPORT": 8,
            "PRECEDENT": 9,
        }

        # Structural families get priority over contextual
        structural_families = {"NEGATIVE", "OBLIGATION", "CRITIQUE", "CALLER", "TEST"}

        def sort_key(item: EvidenceItem) -> tuple[int, int, int]:
            is_structural = 0 if item.family in structural_families else 1
            priority = family_priority.get(item.family, 10)
            return (-item.score, is_structural, priority)

        sorted_items = sorted(candidates, key=sort_key)

        # Apply per-family caps and token budget
        selected: list[EvidenceItem] = []
        family_counts: dict[str, int] = {}
        tokens_used = 0

        for item in sorted_items:
            cap = caps.get(item.family, 1)
            current_count = family_counts.get(item.family, 0)
            if current_count >= cap:
                continue

            # Rough token estimate: ~1 token per 4 chars
            item_tokens = len(item.summary) // 4 + len(item.source_code) // 4 + 10
            if tokens_used + item_tokens > token_budget and selected:
                break

            selected.append(item)
            family_counts[item.family] = current_count + 1
            tokens_used += item_tokens

        return selected

    # ------------------------------------------------------------------
    # Convenience: combined contract + evidence
    # ------------------------------------------------------------------

    def get_guidance(
        self, node_id: int, root: str, token_budget: int = 450
    ) -> tuple[list[ContractRecord], list[EvidenceItem]]:
        """Get both contracts and ranked evidence for a target node.

        This is the main entry point for runtime guidance delivery.
        """
        contracts = self.extract_contracts(node_id)
        raw_evidence = self.compute_evidence(node_id, root)
        selected_evidence = self.rank_and_select(raw_evidence, token_budget)
        return contracts, selected_evidence
