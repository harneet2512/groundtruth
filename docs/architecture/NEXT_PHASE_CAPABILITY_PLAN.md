# Next Phase: Graph Capability Matching + Strengthening

## Context
13-task smoke: 5/13 resolved (4 holds + 1 flip). 0 regressions.
GT caused 1 flip (weasyprint) through L3b caller evidence.
The other flips were model nondeterminism (conan: git history).

Core finding: GT uses the graph as a faster grep. That's 1-2 actions
of speedup — not enough for flips. RepoGraph, CodePlan, and
Codebase-Memory use their graphs for STRUCTURAL INTELLIGENCE that
grep cannot produce. We need to match them.

## Track A: Capability Matching (Python)

### A1: Ego-graph query
Instead of flat caller lists, produce k-hop subgraph centered on a symbol.
k=1: callers + callees + parent class + siblings sharing state.
k=2: callers-of-callers + transitive dependencies.

Research: RepoGraph ICLR 2025 — k=1 gives 11.6 nodes + 37.1 edges avg.
k≥3 adds noise. Render as structured compact text, not prose.

### A2: Change impact analysis
After an edit, trace CalledBy edges transitively to find all impacted
functions. Show the agent: "Your change to X impacts Y (direct caller)
which impacts Z (transitive caller in test)."

Research: CodePlan FSE 2024 — change-may-impact analysis via CalledBy.
5/7 repos pass with propagation, 0/7 without.

### A3: Token-efficient rendering
Current: dump caller lines as text (50-100 tokens per injection).
Target: structured subgraph notation (15-25 tokens per injection).
Format: "X() ← Y():45 [test], Z():120 [controller] | X() → A(), B()"

Research: Codebase-Memory 2026 — 83% quality at 10x fewer tokens.
Du et al. EMNLP 2025 — fewer tokens = better performance.

## Track B: Graph Strengthening (Go indexer)

### B1: Resolution quality
Current: 70-80% of edges are name_match (speculative).
Target: <40% name_match via:
- Scope-aware resolution (class.method qualified names)
- Type inference for return→parameter links
- Self.method() resolution for Python (already in resolver.go:307)

### B2: Containment edges
parent_id exists in nodes table but no CONTAINS edges in edges table.
Add: for each node with parent_id > 0, create CONTAINS edge.
This enables: "what methods are in this class?" as a graph query.

### B3: Community detection
Group functions into cohesive modules by edge density.
Enables: "these 5 functions form a tightly-coupled group — editing
one likely requires changes to the others."

## Proof Plan

Run ego-graph queries on the 13 canary repos' graph.dbs:
- For each gold-file function: produce k=1 ego-graph
- Measure: does the ego-graph contain the gold callers/callees?
- Compare: ego-graph precision vs flat caller list precision
- If ego-graph captures gold relationships better → implement in delivery

## Priority Order

1. A1 (ego-graph) — foundation for A2 and A3
2. A3 (token-efficient rendering) — immediate noise reduction
3. A2 (change impact) — post-edit quality
4. B1 (resolution) — requires Go build, high impact
5. B2 (containment) — small Go change, enables hierarchy queries
6. B3 (community) — research track, validate on holdout first
