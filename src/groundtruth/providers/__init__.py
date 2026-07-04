"""Layer 4 — Evidence Providers (FINAL_ARCH_V2 §3 Layer 4).

Pure functions that answer "given (file, function), what evidence does graph.db
or the repo carry?" — and nothing else.

Provider rules (FINAL_ARCH_V2):
- Providers do NOT know about budgets, iteration bands, viewed files, stale
  state, agent phase, or anything in AgentState.
- Providers do NOT read environment variables.
- Providers do NOT write tmp files.
- Providers do NOT decide whether to emit.
- Providers take inputs, return data. Period.

The Layer 3 Collaboration Router consumes providers and decides timing.
"""

from groundtruth.providers.evidence_providers import (
    CallerCodeRecord,
    Contract,
    EditPropagation,
    SiblingFunction,
    TestAssertion,
    caller_code_provider,
    co_change_provider,
    contract_provider,
    edit_propagation_provider,
    sibling_twin_provider,
    structural_twin_in_function_provider,
    test_provider,
)
from groundtruth.providers.graph_providers import (
    CalleeEdge,
    CallerEdge,
    FunctionInfo,
    ImporterEdge,
    callee_provider,
    caller_provider,
    hub_scale_provider,
    importer_provider,
    in_degree_provider,
    top_functions_provider,
)
from groundtruth.providers.scoring import (
    issue_relevance_scorer,
    score_edges_by_issue_relevance,
)

__all__ = [
    "CalleeEdge",
    "CallerCodeRecord",
    "CallerEdge",
    "Contract",
    "EditPropagation",
    "FunctionInfo",
    "ImporterEdge",
    "SiblingFunction",
    "TestAssertion",
    "callee_provider",
    "caller_code_provider",
    "caller_provider",
    "co_change_provider",
    "contract_provider",
    "edit_propagation_provider",
    "hub_scale_provider",
    "importer_provider",
    "in_degree_provider",
    "issue_relevance_scorer",
    "score_edges_by_issue_relevance",
    "sibling_twin_provider",
    "structural_twin_in_function_provider",
    "test_provider",
    "top_functions_provider",
]
