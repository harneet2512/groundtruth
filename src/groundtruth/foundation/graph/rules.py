"""Built-in expansion rules for the graph expander."""

from __future__ import annotations

from groundtruth.foundation.graph.expander import ExpansionRule

# --- Built-in rules ---

CALLERS = ExpansionRule(
    edge_type="caller",
    direction="incoming",
    max_per_seed=5,
    weight=0.8,
)

CALLEES = ExpansionRule(
    edge_type="callee",
    direction="outgoing",
    max_per_seed=5,
    weight=0.6,
)

SAME_CLASS = ExpansionRule(
    edge_type="same_class",
    direction="membership",
    max_per_seed=10,
    weight=0.7,
)

IMPORT_DEPENDENTS = ExpansionRule(
    edge_type="import_dependents",
    direction="incoming",
    max_per_seed=5,
    weight=0.5,
)

CONSTRUCTOR_PAIR = ExpansionRule(
    edge_type="constructor_pair",
    direction="membership",
    max_per_seed=8,
    weight=0.9,
)

OVERRIDE_CHAIN = ExpansionRule(
    edge_type="override_chain",
    direction="both",
    max_per_seed=3,
    weight=0.7,
)

SHARED_STATE = ExpansionRule(
    edge_type="shared_state",
    direction="membership",
    max_per_seed=5,
    weight=0.8,
)

# Convenience: all rules in default priority order
ALL_RULES: list[ExpansionRule] = [
    CONSTRUCTOR_PAIR,  # weight=0.9, highest
    CALLERS,           # weight=0.8
    SHARED_STATE,      # weight=0.8
    SAME_CLASS,        # weight=0.7
    OVERRIDE_CHAIN,    # weight=0.7
    CALLEES,           # weight=0.6
    IMPORT_DEPENDENTS, # weight=0.5
]
