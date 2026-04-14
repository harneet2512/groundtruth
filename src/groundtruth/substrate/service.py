"""SubstrateService -- canonical entry point for GT semantics."""

from __future__ import annotations

import re

from groundtruth.contracts.engine import ContractEngine
from groundtruth.contracts.repo_coupling import build_repo_coupling_contracts
from groundtruth.substrate.protocols import ContractExtractor, EvidenceProducer, GraphReader
from groundtruth.substrate.types import (
    ContractBundle,
    ContractRecord,
    ConstraintHint,
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
        # Infer bug mechanism from dominant structural signal
        if targets and targets[0].reasons:
            top_reasons = targets[0].reasons
            if any("test_proximity" in r or "assertion_proximity" in r for r in top_reasons):
                likely_bug_mechanism = "test_visible_regression"
            elif any("call_graph" in r for r in top_reasons):
                likely_bug_mechanism = "caller_contract_violation"
            elif any("contract_density" in r for r in top_reasons):
                likely_bug_mechanism = "contract_or_behavior_regression"
            elif any("sibling_match" in r for r in top_reasons):
                likely_bug_mechanism = "paired_or_sibling_inconsistency"
            elif any("qualified_name" in r for r in top_reasons):
                likely_bug_mechanism = "qualified_symbol_mismatch"
            else:
                likely_bug_mechanism = "structural_regression"
        elif bundle.contracts:
            likely_bug_mechanism = "contract_or_behavior_regression"
        else:
            likely_bug_mechanism = "broad_search"
        repro_hint = f"Search tests mentioning: {identifiers[0]}" if identifiers else None

        semantic_constraints = self._build_semantic_constraints(bundle.contracts, confidence)

        return RepoIntelBrief(
            top_candidate_file=top_file,
            backup_files=backup_files,
            candidate_symbols=candidate_symbols,
            likely_bug_mechanism=likely_bug_mechanism,
            do_not_break=do_not_break,
            repro_hint=repro_hint,
            confidence=confidence,
            issue_identifiers=identifiers,
            semantic_constraints=semantic_constraints,
        )

    def _build_semantic_constraints(
        self,
        contracts: tuple[ContractRecord, ...],
        confidence: str,
    ) -> tuple[ConstraintHint, ...]:
        """Select up to 3 'must remain true' constraints for pre-edit delivery.

        Rendering rules:
        - ONLY vNext families (behavioral_assertion, paired_behavior,
          constructor_postcondition, dispatch_registration) participate
        - verified contracts ranked first, then top 1 likely if no verified
        - checkable=True contracts ranked before checkable=False within tier
        - Maximum 3 constraints total
        - If confidence is medium or broad_search: return empty (no false precision)
        """
        from groundtruth.contracts.engine import VNEXT_CONTRACT_TYPES

        if confidence not in ("high",):
            return ()

        # Filter to vNext families only — legacy families must not steer
        vnext_contracts = [c for c in contracts if c.contract_type in VNEXT_CONTRACT_TYPES]

        def rank_key(c: ContractRecord) -> tuple[int, int, float]:
            tier_order = {"verified": 0, "likely": 1, "possible": 2}
            checkable_order = 0 if c.checkable else 1
            return (tier_order.get(c.tier, 2), checkable_order, -c.confidence)

        sorted_contracts = sorted(vnext_contracts, key=rank_key)

        # Take up to 3: all verified + top 1 likely (if no verified available)
        selected: list[ContractRecord] = []
        has_verified = any(c.tier == "verified" for c in sorted_contracts)

        for contract in sorted_contracts:
            if len(selected) >= 3:
                break
            if contract.tier == "verified":
                selected.append(contract)
            elif contract.tier == "likely" and not has_verified and len(selected) < 1:
                selected.append(contract)

        hints: list[ConstraintHint] = []
        for c in selected:
            source_count = c.support_count
            support_summary = (
                f"{source_count} source(s): {', '.join(c.support_kinds[:2])}"
                if c.support_kinds
                else f"{source_count} source(s)"
            )
            hints.append(
                ConstraintHint(
                    text=c.predicate,
                    tier=c.tier,
                    family=c.contract_type,
                    checkable=c.checkable,
                    support_summary=support_summary,
                )
            )

        return tuple(hints)

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
            short_name = ref.split(".")[-1] if "." in ref else ref
            node = None
            # File-scoped resolution first: when changed_files are known, use them
            # to resolve ambiguous symbols (e.g. __array_ufunc__ exists in many files).
            for fp in changed_files:
                node = self._reader.get_node_by_name(short_name, file_path=fp)
                if node:
                    break
            # Global resolution fallback (only works when symbol is unambiguous).
            if not node:
                node = self._reader.get_node_by_name(ref)
            if not node and "." in ref:
                node = self._reader.get_node_by_name(short_name)
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
        """Structural localization: rank candidates by combined graph signals.

        Signals (weighted):
          0.30  issue_identifier — symbol name matches issue text
          0.25  test_proximity   — nearby tests/assertions reference this symbol
          0.20  call_graph       — callers/callees connected to issue symbols
          0.10  pair_or_sibling  — paired/sibling relation to issue symbols
          0.10  contract_density — symbols with dense vNext contracts
          0.05  file_prior       — file hotspot bonus (many symbols from same file)
        """
        candidates: list[dict] = []
        seen_ids: set[int] = set()
        ident_set = set(identifiers)
        ident_lower = {i.lower() for i in identifiers}

        # Collect all candidate nodes from identifiers
        for ident in identifiers:
            node = self._reader.get_node_by_name(ident)
            if not node and "." in ident:
                node = self._reader.get_node_by_name(ident.split(".")[-1])
            if not node:
                continue
            node_id = node.get("id")
            if node_id is None or node_id in seen_ids:
                continue
            seen_ids.add(node_id)
            candidates.append({"node": node, "ident": ident, "node_id": node_id})

        if not candidates:
            return ()

        # Score each candidate
        scored: list[tuple[float, LocalizationTarget]] = []
        file_counts: dict[str, int] = {}

        for cand in candidates:
            node = cand["node"]
            node_id = cand["node_id"]
            ident = cand["ident"]
            name = node.get("qualified_name") or node.get("name") or ident
            file_path = node.get("file_path") or ""

            reasons: list[str] = []

            # Signal 1: issue_identifier (0.30)
            if ident == (node.get("qualified_name") or ""):
                id_score = 1.0
                reasons.append("qualified_name_match")
            elif ident == node.get("name"):
                id_score = 0.8
                reasons.append("exact_name_match")
            else:
                id_score = 0.5
                reasons.append("partial_name_match")

            # Signal 2: test_proximity (0.25)
            test_score = 0.0
            try:
                tests = self._reader.get_tests_for(node_id)
                if tests:
                    test_score = min(1.0, len(tests) / 3.0)
                    reasons.append(f"test_proximity({len(tests)})")
                else:
                    assertions = self._reader.get_assertions_for_target(node.get("name", ""))
                    if assertions:
                        test_score = min(1.0, len(assertions) / 5.0)
                        reasons.append(f"assertion_proximity({len(assertions)})")
            except Exception:
                pass

            # Signal 3: call_graph (0.20)
            cg_score = 0.0
            try:
                callers = self._reader.get_callers(node_id)
                callees = self._reader.get_callees(node_id)
                # Bonus if callers/callees themselves match issue identifiers
                connected_idents = sum(
                    1 for c in callers
                    if c.get("name", "").lower() in ident_lower
                    or any(i.lower() in (c.get("name", "").lower()) for i in identifiers[:3])
                )
                caller_count = len(callers)
                if caller_count > 0:
                    cg_score = min(1.0, (caller_count + connected_idents * 2) / 10.0)
                    reasons.append(f"call_graph(callers={caller_count},connected={connected_idents})")
            except Exception:
                pass

            # Signal 4: pair_or_sibling (0.10)
            sib_score = 0.0
            try:
                siblings = self._reader.get_siblings(node_id)
                # Check if any sibling name appears in identifiers
                sib_matches = sum(
                    1 for s in siblings
                    if s.get("name", "").lower() in ident_lower
                )
                if sib_matches > 0:
                    sib_score = min(1.0, sib_matches / 2.0)
                    reasons.append(f"sibling_match({sib_matches})")
                elif len(siblings) > 5:
                    sib_score = 0.2  # Mild boost for being in a large class
            except Exception:
                pass

            # Signal 5: contract_density (0.10)
            contract_score = 0.0
            try:
                contracts = self._engine.extract_all(node_id)
                if contracts:
                    from groundtruth.contracts.engine import VNEXT_CONTRACT_TYPES
                    vnext_count = sum(1 for c in contracts if c.contract_type in VNEXT_CONTRACT_TYPES)
                    contract_score = min(1.0, vnext_count / 3.0)
                    if vnext_count > 0:
                        reasons.append(f"contract_density({vnext_count})")
            except Exception:
                pass

            # Signal 6: file_prior (0.05)
            file_counts[file_path] = file_counts.get(file_path, 0) + 1
            file_score = 0.5  # Base; boosted later if multiple symbols from same file

            # Composite score
            final_score = (
                0.30 * id_score
                + 0.25 * test_score
                + 0.20 * cg_score
                + 0.10 * sib_score
                + 0.10 * contract_score
                + 0.05 * file_score
            )

            # Tier from structural score
            if final_score >= 0.50 and len(reasons) >= 3:
                tier = "verified"
            elif final_score >= 0.30 and len(reasons) >= 2:
                tier = "likely"
            else:
                tier = "possible"

            scored.append((
                final_score,
                LocalizationTarget(
                    node_id=node_id,
                    name=name,
                    file_path=file_path,
                    start_line=node.get("start_line") or 0,
                    confidence=final_score,
                    tier=tier,
                    file_confidence=min(1.0, final_score + 0.05),
                    symbol_confidence=final_score,
                    reasons=tuple(reasons),
                ),
            ))

        # Boost file_prior for symbols sharing a file with other candidates
        for i, (score, target) in enumerate(scored):
            if file_counts.get(target.file_path, 0) > 1:
                boost = 0.05 * (file_counts[target.file_path] - 1)
                scored[i] = (score + boost, target)

        scored.sort(key=lambda x: -x[0])
        return tuple(t for _, t in scored)
