"""CollaborationRouter — FINAL_ARCH_V2 Layer 3.

The router takes the canonical ``AgentState`` (Layer 2) and provider outputs
(Layer 4) and decides:

- on_view(observed_file): emit a graph-navigation hint, or suppress with a
  reason that the metric script can attribute.
- on_edit(edit_target, function_names): emit a contract/twin/caller hint, or
  suppress with a reason.

The router is intentionally narrow this session. It does not (yet):

- drive the live wrapper (shadow-mode only)
- emit post-edit warnings (those live in Layer 5
  ``validators/post_edit_validator.py`` and are forwarded by callers, not
  generated here)
- contradict-classify ignored suggestions (that needs richer state)

Budgets and debounce follow Decision 34 §12.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from groundtruth.providers import (
    caller_code_provider,
    caller_provider,
    callee_provider,
    contract_provider,
    edit_propagation_provider,
    importer_provider,
    score_edges_by_issue_relevance,
    sibling_twin_provider,
    test_provider,
)
from groundtruth.router.decisions import (
    EmissionKind,
    RouterEmission,
    SuppressionReason,
)
from groundtruth.state.agent_state import AgentState, IterationBand, canonical_repo_path

if TYPE_CHECKING:  # pragma: no cover
    pass


# Default budgets (per Decision 35 + Decision 34 §12). These mirror what the
# live wrapper enforces today; centralizing them here means later changes to
# context-budget policy live in one place.
DEFAULT_TOTAL_BUDGET = 8  # safety ceiling (D34 §12 preserved); first-per-file is primary gate
AMBIGUITY_MARGIN = 0.12  # downgrade to soft when top-2 within this margin


class CollaborationRouter:
    """Decides WHEN to emit Layer 4 evidence to the agent."""

    def __init__(
        self,
        state: AgentState,
        db_path: str,
        repo_root: str = "/testbed",
        *,
        total_budget: int = DEFAULT_TOTAL_BUDGET,
        late_band_ratio: float = 0.75,
        delegate_evidence: bool = False,
    ) -> None:
        import os as _os
        self.state = state
        self.db_path = db_path
        self.repo_root = repo_root
        self.total_budget = total_budget
        self.late_band_ratio = late_band_ratio
        self.delegate_evidence = delegate_evidence
        self._graph_db_present = delegate_evidence or (bool(db_path) and _os.path.isfile(db_path))
        self._total_emits = 0
        self._last_emit_kind: EmissionKind | None = None
        self._last_emit_iter: int = -100
        self._emitted_target_keys: set[str] = set()  # first-per-file + novelty fingerprint
        self._pending_ids_registered: set[str] = set()
        self.debounce_iters = 0
        self.provider_request_count = 0
        self.provider_empty_count = 0
        self.provider_request_log: list[dict[str, object]] = []

    # ---- public API ---------------------------------------------------

    def on_view(self, observed_file: str) -> RouterEmission:
        """Decide what to render when the agent reads ``observed_file``."""
        em = self._new_emission(
            kind=EmissionKind.ON_VIEW_NEIGHBORHOOD,
            target_file=observed_file,
        )
        canon = canonical_repo_path(observed_file, self.repo_root)
        if not canon:
            return self._suppress(em, SuppressionReason.NO_EVIDENCE, "empty_path")

        # NO_GRAPH_DB short-circuits before budget/dedup. The router cannot
        # emit when the graph itself is unavailable, but we still want this
        # classified separately from "graph present, no evidence for this
        # file" so the shadow-replay report can attribute the miss correctly.
        if not self._graph_db_present:
            return self._suppress(em, SuppressionReason.NO_GRAPH_DB, "graph_db_missing")

        # First-per-file: suppress re-reads (view dedup separate from edit dedup)
        if f"view::{canon}" in self._emitted_target_keys:
            return self._suppress(em, SuppressionReason.DUPLICATE, "file_already_briefed")
        # Total ceiling (D34 §12 safety valve)
        if self._total_emits >= self.total_budget:
            return self._suppress(em, SuppressionReason.BUDGET, "total_budget_reached")

        # Iteration-band gate. After 75% of max iter, do not emit broad nav.
        if self._is_late_band():
            return self._suppress(em, SuppressionReason.TOO_LATE, f"band={em.band}")

        # Debounce same-kind emission.
        if self._debounced(EmissionKind.ON_VIEW_NEIGHBORHOOD):
            return self._suppress(em, SuppressionReason.DEBOUNCE, "same_kind_recent")

        # In delegate mode, the router only gates on budget/debounce/band.
        # Evidence comes from the in-container hook; the router doesn't query
        # graph.db at all. The wrapper checks evidence markers after the hook
        # runs and only injects if real evidence was produced.
        if self.delegate_evidence:
            dedup_key = f"view::{canon}"
            if dedup_key in self._emitted_target_keys:
                return self._suppress(em, SuppressionReason.DUPLICATE, "same_view")
            em.evidence_text = "(delegated to in-container hook)"
            return self._accept(em, dedup_key)

        # Pull graph providers.
        self.provider_request_count += 1
        callers = caller_provider(self.db_path, canon, limit=5)
        callees = callee_provider(self.db_path, canon, limit=5)
        importers = importer_provider(self.db_path, canon, limit=3)
        self.provider_request_log.append({
            "kind": "on_view",
            "file": canon,
            "callers": len(callers),
            "callees": len(callees),
            "importers": len(importers),
        })
        if not (callers or callees or importers):
            self.provider_empty_count += 1
            return self._suppress(em, SuppressionReason.NO_EVIDENCE, "graph_empty_for_file")

        # Drop edges that point at already-visited files (stale neighbors).
        visited = self.state.visited_files_set()
        unseen_callers = [c for c in callers if canonical_repo_path(c.file_path, self.repo_root) not in visited]
        unseen_callees = [c for c in callees if canonical_repo_path(c.file_path, self.repo_root) not in visited]
        unseen_importers = [
            i for i in importers if canonical_repo_path(i.file_path, self.repo_root) not in visited
        ]
        if not (unseen_callers or unseen_callees or unseen_importers):
            return self._suppress(em, SuppressionReason.STALE, "all_neighbors_visited")

        # Issue-relevance ranking on whichever edge group is present.
        issue_terms = self.state.issue_terms
        if unseen_callers:
            ranked = score_edges_by_issue_relevance(
                [(c.file_path, c.count) for c in unseen_callers],
                self.repo_root,
                issue_terms,
            )
            primary = ranked[0]
            em.primary_edge_file = primary[0]
            em.next_action_type = "READ_CALLER_CONTRACT"
            em.next_action_file = primary[0]
        elif unseen_callees:
            ranked = score_edges_by_issue_relevance(
                [(c.file_path, c.count) for c in unseen_callees],
                self.repo_root,
                issue_terms,
            )
            primary = ranked[0]
            em.primary_edge_file = primary[0]
            em.next_action_type = "READ_CONSUMER"
            em.next_action_file = primary[0]
        else:
            em.primary_edge_file = unseen_importers[0].file_path
            em.next_action_type = "READ_CONSUMER"
            em.next_action_file = unseen_importers[0].file_path

        # Deduplicate against already-emitted (target_file, primary_edge_file).
        dedup_key = f"view::{canon}::{em.primary_edge_file}"
        if dedup_key in self._emitted_target_keys:
            return self._suppress(em, SuppressionReason.DUPLICATE, "same_view_and_primary")

        # Build a compact evidence string.
        em.evidence_text = self._format_view_evidence(canon, unseen_callers, unseen_callees, unseen_importers)
        em.evidence_items = [
            {"kind": "caller_edge", "file_path": c.file_path, "count": c.count}
            for c in unseen_callers[:3]
        ] + [
            {"kind": "callee_edge", "file_path": c.file_path, "count": c.count}
            for c in unseen_callees[:3]
        ] + [
            {"kind": "importer_edge", "file_path": i.file_path}
            for i in unseen_importers[:3]
        ]
        em.confidence = 1.0 if unseen_callers or unseen_callees else 0.6

        return self._accept(em, dedup_key)

    def on_edit(self, edit_target: str, function_names: list[str]) -> RouterEmission:
        """Decide what to render when the agent edits ``edit_target``."""
        em = self._new_emission(
            kind=EmissionKind.ON_EDIT_CONTRACT,
            target_file=edit_target,
            target_functions=function_names,
        )
        canon = canonical_repo_path(edit_target, self.repo_root)
        if not canon:
            return self._suppress(em, SuppressionReason.NO_EVIDENCE, "empty_path")

        # NO_GRAPH_DB before everything else (mirror on_view).
        if not self._graph_db_present:
            return self._suppress(em, SuppressionReason.NO_GRAPH_DB, "graph_db_missing")

        # First-per-file: suppress if this file already got EDIT evidence
        # (separate from view dedup — reading a file doesn't count as edit evidence)
        if f"edit::{canon}" in self._emitted_target_keys:
            return self._suppress(em, SuppressionReason.DUPLICATE, "file_already_briefed")
        # Total ceiling (D34 §12 safety valve)
        if self._total_emits >= self.total_budget:
            return self._suppress(em, SuppressionReason.BUDGET, "total_budget_reached")

        if self._debounced(EmissionKind.ON_EDIT_CONTRACT):
            return self._suppress(em, SuppressionReason.DEBOUNCE, "same_kind_recent")

        if self.delegate_evidence:
            dedup_key = f"edit::{canon}"
            if dedup_key in self._emitted_target_keys:
                return self._suppress(em, SuppressionReason.DUPLICATE, "same_edit")
            em.evidence_text = "(delegated to in-container hook)"
            return self._accept(em, dedup_key, kind=EmissionKind.ON_EDIT_CONTRACT)

        if not function_names:
            return self._suppress(em, SuppressionReason.NO_EVIDENCE, "no_function_target")

        # Build evidence from providers. Keep priority order matching D1
        # LOCKED: caller code > sibling > contract > test.
        self.provider_request_count += 1
        seen_files = sorted(self.state.visited_files_set())
        items: list[dict[str, object]] = []
        ev_text_lines: list[str] = []
        any_caller = False
        for fn in function_names[:3]:
            callers = caller_code_provider(
                self.db_path, canon, fn, self.repo_root,
                seen_files=seen_files, limit=3,
            )
            if callers:
                any_caller = True
                items.extend(
                    {
                        "kind": "caller_code",
                        "function": fn,
                        "caller_file": c.file,
                        "caller_line": c.line,
                        "code": c.code,
                        "unseen": c.unseen,
                    }
                    for c in callers
                )
                ev_text_lines.append(
                    f"CALLERS of {fn}: "
                    + ", ".join(f"{c.file}:{c.line}" for c in callers[:3])
                )
            contract = contract_provider(self.db_path, canon, fn)
            if contract:
                items.append(
                    {
                        "kind": "contract",
                        "function": fn,
                        "signature": contract.signature,
                        "return_type": contract.return_type,
                    }
                )
                if contract.signature:
                    ev_text_lines.append(f"SIGNATURE {fn}: {contract.signature}")
            sibs = sibling_twin_provider(self.db_path, canon, fn, self.repo_root)
            if sibs:
                items.extend(
                    {
                        "kind": "sibling",
                        "function": fn,
                        "sibling_name": s.name,
                        "signature": s.signature,
                        "snippet": s.snippet,
                    }
                    for s in sibs[:2]
                )
            tests = test_provider(self.db_path, canon, fn)
            if tests:
                items.extend(
                    {
                        "kind": "test_assertion",
                        "function": fn,
                        "test_name": t.test_name,
                        "expression": t.expression,
                        "expected": t.expected,
                    }
                    for t in tests[:2]
                )

        self.provider_request_log.append({
            "kind": "on_edit",
            "file": canon,
            "functions": list(function_names[:3]),
            "items": len(items),
            "any_caller": any_caller,
        })
        if not items:
            self.provider_empty_count += 1
            return self._suppress(em, SuppressionReason.NO_EVIDENCE, "all_providers_empty")
        if not any_caller and not any(it["kind"] == "contract" for it in items):
            # Without callers OR a contract, there is nothing actionable for
            # the agent to verify against. Stay silent — this is the rule that
            # replaces today's "fire on every edit" behavior.
            return self._suppress(em, SuppressionReason.LOW_CONFIDENCE, "no_caller_or_contract")

        # Edit propagation hint (optional, low-noise).
        for fn in function_names[:2]:
            propagation = edit_propagation_provider(self.db_path, canon, fn, limit=3)
            if propagation:
                items.extend(
                    {"kind": "propagation", "function": fn, "caller_file": p.caller_file, "line": p.line}
                    for p in propagation
                )

        dedup_key = f"edit::{canon}::{','.join(function_names)}"
        if dedup_key in self._emitted_target_keys:
            return self._suppress(em, SuppressionReason.DUPLICATE, "same_edit_target")

        em.evidence_items = items
        em.evidence_text = "\n".join(ev_text_lines)
        em.primary_edge_file = self._first_caller_file(items)
        em.next_action_type = "READ_CALLER_CONTRACT" if any_caller else "CHECK_SIGNATURE"
        em.next_action_file = em.primary_edge_file
        em.confidence = 1.0 if any_caller else 0.7

        return self._accept(em, dedup_key, kind=EmissionKind.ON_EDIT_CONTRACT)

    # ---- helpers ------------------------------------------------------

    def _new_emission(
        self,
        *,
        kind: EmissionKind,
        target_file: str,
        target_functions: list[str] | None = None,
    ) -> RouterEmission:
        return RouterEmission(
            kind=kind,
            emit=False,
            suppression_reason=SuppressionReason.NOT_APPLICABLE,
            target_file=target_file,
            target_functions=list(target_functions or []),
            iteration=self.state.iteration,
            band=self.state.band.value if isinstance(self.state.band, IterationBand) else str(self.state.band),
        )

    def _suppress(
        self, em: RouterEmission, reason: SuppressionReason, detail: str
    ) -> RouterEmission:
        em.emit = False
        em.suppression_reason = reason
        em.suppression_detail = detail
        return em

    def _accept(
        self,
        em: RouterEmission,
        dedup_key: str,
        *,
        kind: EmissionKind | None = None,
    ) -> RouterEmission:
        em.emit = True
        em.suppression_reason = None
        if kind is None:
            kind = em.kind
        else:
            em.kind = kind
        self._emitted_target_keys.add(dedup_key)
        self._last_emit_kind = kind
        self._last_emit_iter = self.state.iteration
        self._total_emits += 1
        # Track per-kind dedup keys (view and edit separated)
        if em.target_file:
            file_canon = canonical_repo_path(em.target_file, self.repo_root)
            if file_canon:
                prefix = "edit" if kind == EmissionKind.ON_EDIT_CONTRACT else "view"
                self._emitted_target_keys.add(f"{prefix}::{file_canon}")
        return em

    def _is_late_band(self) -> bool:
        if self.state.max_iterations <= 0:
            return False
        ratio = self.state.iteration / self.state.max_iterations
        return ratio >= self.late_band_ratio

    def _debounced(self, kind: EmissionKind) -> bool:
        if self._last_emit_kind != kind:
            return False
        return (self.state.iteration - self._last_emit_iter) < self.debounce_iters

    @staticmethod
    def _first_caller_file(items: list[dict[str, object]]) -> str:
        for it in items:
            if it.get("kind") == "caller_code" and it.get("caller_file"):
                return str(it["caller_file"])
        return ""

    @staticmethod
    def _format_view_evidence(
        observed: str,
        callers: list[object],
        callees: list[object],
        importers: list[object],
    ) -> str:
        parts: list[str] = []
        if callers:
            parts.append("Called by: " + ", ".join(f"{c.file_path} ({c.count}x)" for c in callers[:3]))  # type: ignore[attr-defined]
        if callees:
            parts.append("Calls into: " + ", ".join(f"{c.file_path} ({c.count}x)" for c in callees[:3]))  # type: ignore[attr-defined]
        if importers:
            parts.append("Imported by: " + ", ".join(i.file_path for i in importers[:3]))  # type: ignore[attr-defined]
        return "\n".join(parts)


__all__ = ["CollaborationRouter"]
