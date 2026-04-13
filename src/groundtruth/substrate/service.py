"""SubstrateService -- canonical entry point for GT semantics."""

from __future__ import annotations

import re

from groundtruth.contracts.engine import ContractEngine
from groundtruth.contracts.repo_coupling import build_repo_coupling_contracts
from groundtruth.substrate.protocols import ContractExtractor, EvidenceProducer, GraphReader
from groundtruth.substrate.types import (
    ContractBundle,
    ContractRecord,
    EvidenceItem,
    LocalizationTarget,
    PatchVerdict,
    RepoIntelBrief,
    ResolutionResult,
)
from groundtruth.verification.models import PatchCandidate
from groundtruth.verification.patch_scorer import PatchScorer


class SubstrateService:
    """Single semantic entry point used by hooks, MCP, and adapters."""

    def __init__(self, reader: GraphReader) -> None:
        self._reader = reader
        self._engine = ContractEngine(reader)
        self._producers: list[EvidenceProducer] = []
        self._patch_scorer = PatchScorer(reader)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_extractor(self, extractor: ContractExtractor) -> None:
        self._engine.register_extractor(extractor)

    def register_producer(self, producer: EvidenceProducer) -> None:
        self._producers.append(producer)

    # ------------------------------------------------------------------
    # Contract extraction
    # ------------------------------------------------------------------

    def extract_contracts(self, node_id: int) -> list[ContractRecord]:
        return self._engine.extract_all(node_id)

    # ------------------------------------------------------------------
    # Evidence production
    # ------------------------------------------------------------------

    def compute_evidence(self, node_id: int, root: str) -> list[EvidenceItem]:
        results: list[EvidenceItem] = []
        for producer in self._producers:
            try:
                results.extend(producer.produce(self._reader, node_id, root))
            except Exception:
                continue
        return results

    def rank_and_select(
        self,
        candidates: list[EvidenceItem],
        token_budget: int = 450,
        per_family_caps: dict[str, int] | None = None,
    ) -> list[EvidenceItem]:
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
        structural_families = {"NEGATIVE", "OBLIGATION", "CRITIQUE", "CALLER", "TEST"}

        def sort_key(item: EvidenceItem) -> tuple[int, int, int]:
            is_structural = 0 if item.family in structural_families else 1
            priority = family_priority.get(item.family, 10)
            return (-item.score, is_structural, priority)

        selected: list[EvidenceItem] = []
        family_counts: dict[str, int] = {}
        tokens_used = 0
        for item in sorted(candidates, key=sort_key):
            cap = caps.get(item.family, 1)
            current_count = family_counts.get(item.family, 0)
            if current_count >= cap:
                continue
            item_tokens = len(item.summary) // 4 + len(item.source_code) // 4 + 10
            if tokens_used + item_tokens > token_budget and selected:
                break
            selected.append(item)
            family_counts[item.family] = current_count + 1
            tokens_used += item_tokens
        return selected

    def get_guidance(
        self, node_id: int, root: str, token_budget: int = 450
    ) -> tuple[list[ContractRecord], list[EvidenceItem]]:
        contracts = self.extract_contracts(node_id)
        evidence = self.rank_and_select(self.compute_evidence(node_id, root), token_budget)
        return contracts, evidence

    # ------------------------------------------------------------------
    # Canonical service boundary
    # ------------------------------------------------------------------

    def resolve_symbol(self, symbol: str, file_path: str | None = None) -> ResolutionResult:
        node = self._reader.get_node_by_name(symbol, file_path)
        if not node:
            return ResolutionResult(symbol=symbol, status="missing", matches=())
        return ResolutionResult(
            symbol=symbol,
            status="resolved",
            matches=((node.get("qualified_name") or node.get("name") or symbol, node.get("file_path") or ""),),
            resolved_node_id=node.get("id"),
            resolved_file=node.get("file_path"),
            qualified_name=node.get("qualified_name") or node.get("name"),
        )

    def get_repo_intel(
        self,
        issue_text: str,
        root_path: str,
        changed_files: tuple[str, ...] = (),
    ) -> RepoIntelBrief:
        identifiers = tuple(self._extract_issue_identifiers(issue_text))
        targets = self._rank_localization_targets(identifiers)
        top_file = targets[0].file_path if targets else (changed_files[0] if changed_files else None)
        backup_files = tuple(dict.fromkeys(t.file_path for t in targets[1:3]))
        candidate_symbols = tuple(t.name for t in targets[:3])
        confidence = "broad_search"
        if targets and targets[0].tier == "verified":
            confidence = "high"
        elif targets:
            confidence = "medium"

        bundle = self.get_contracts(candidate_symbols[:1], changed_files=changed_files, root_path=root_path)
        do_not_break = tuple(contract.predicate for contract in bundle.contracts[:2])
        likely_bug_mechanism = (
            "qualified_symbol_mismatch"
            if targets and "." in targets[0].name
            else "contract_or_behavior_regression"
            if bundle.contracts
            else "broad_search"
        )
        repro_hint = f"Search tests mentioning: {identifiers[0]}" if identifiers else None

        return RepoIntelBrief(
            top_candidate_file=top_file,
            backup_files=backup_files,
            candidate_symbols=candidate_symbols,
            likely_bug_mechanism=likely_bug_mechanism,
            do_not_break=do_not_break,
            repro_hint=repro_hint,
            confidence=confidence,
            issue_identifiers=identifiers,
        )

    def get_contracts(
        self,
        scope_refs: tuple[str, ...] | list[str],
        changed_files: tuple[str, ...] = (),
        root_path: str | None = None,
    ) -> ContractBundle:
        contracts: list[ContractRecord] = []
        seen_nodes: set[int] = set()

        for ref in scope_refs:
            if not ref:
                continue
            node = self._reader.get_node_by_name(ref)
            if not node and "." in ref:
                node = self._reader.get_node_by_name(ref.split(".")[-1])
            if not node:
                continue
            node_id = node.get("id")
            if node_id in seen_nodes:
                continue
            seen_nodes.add(node_id)
            contracts.extend(self.extract_contracts(node_id))

        if root_path and changed_files:
            contracts.extend(build_repo_coupling_contracts(self._reader, root_path, list(changed_files)))

        return ContractBundle(
            contracts=tuple(contracts),
            scope_refs=tuple(scope_refs),
            changed_files=tuple(changed_files),
            freshness_state="fresh" if contracts else "unknown",
        )

    def score_patch(
        self,
        diff: str,
        changed_files: tuple[str, ...],
        changed_symbols: tuple[str, ...],
        task_ref: str = "substrate",
        candidate_id: str = "working_tree",
        root_path: str | None = None,
    ) -> PatchVerdict:
        bundle = self.get_contracts(changed_symbols, changed_files=changed_files, root_path=root_path)
        candidate = PatchCandidate(
            task_ref=task_ref,
            candidate_id=candidate_id,
            diff=diff,
            changed_files=changed_files,
            changed_symbols=changed_symbols,
        )
        result = self._patch_scorer.score(candidate, list(bundle.contracts))
        return PatchVerdict(
            decision=result.decision,
            overall_score=result.overall_score,
            contract_score=result.contract_score,
            test_score=result.test_score,
            maintainability_score=result.maintainability_score,
            hard_violations=tuple(v.explanation for v in result.hard_violations),
            soft_warnings=tuple(v.explanation for v in result.soft_warnings),
            abstentions=result.abstentions,
            reason_codes=result.reason_codes,
            recommended_next_check=result.recommended_next_check,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _extract_issue_identifiers(self, issue_text: str) -> list[str]:
        identifiers: list[str] = []
        for backtick in re.findall(r"`([^`]+)`", issue_text):
            cleaned = backtick.strip()
            if cleaned and cleaned not in identifiers:
                identifiers.append(cleaned)
        for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_.]{2,}\b", issue_text):
            if token.lower() in {"error", "issue", "return", "value", "false", "true"}:
                continue
            if token not in identifiers:
                identifiers.append(token)
        return identifiers[:8]

    def _rank_localization_targets(self, identifiers: tuple[str, ...]) -> tuple[LocalizationTarget, ...]:
        targets: list[LocalizationTarget] = []
        seen_ids: set[int] = set()
        for ident in identifiers:
            node = self._reader.get_node_by_name(ident)
            if not node and "." in ident:
                node = self._reader.get_node_by_name(ident.split(".")[-1])
            if not node:
                continue
            node_id = node.get("id")
            if node_id in seen_ids:
                continue
            seen_ids.add(node_id)
            confidence = 0.9 if ident == (node.get("qualified_name") or "") else 0.8
            tier = "verified" if confidence >= 0.85 else "likely"
            targets.append(
                LocalizationTarget(
                    node_id=node_id,
                    name=node.get("qualified_name") or node.get("name") or ident,
                    file_path=node.get("file_path") or "",
                    start_line=node.get("start_line") or 0,
                    confidence=confidence,
                    tier=tier,
                    file_confidence=min(1.0, confidence + 0.05),
                    symbol_confidence=confidence,
                    reasons=("issue_identifier",),
                )
            )
        return tuple(sorted(targets, key=lambda t: (-t.confidence, t.file_path, t.name)))
