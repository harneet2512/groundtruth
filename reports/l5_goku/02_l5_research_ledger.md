# L5 Research Ledger — 2026-05-15

## Citations

| # | Source | Venue/Year | Key Finding | L5 Implication | Confidence |
|---|--------|------------|-------------|----------------|------------|
| 1 | SWE-agent ACI (Yang et al.) | NeurIPS 2024 | ACI design > model capability | L5 = ACI element, 180-token cap | HIGH |
| 2 | Agentless (Xia et al.) | ICLR 2025 | No-test validation, syntax+regression | L5 validates without test failures | HIGH |
| 3 | SWE-Pruner | arXiv 2025 | Less context = better (64% vs 62%) | file_kind/check_kind for pruning | HIGH |
| 4 | RepoGraph (Ouyang et al.) | ICLR 2025 | k-hop ego-graphs, +32.8% | Structural witnesses from callers (L3/L3b) | HIGH |
| 5 | FeedbackEval | arXiv 2025 | Mixed feedback 63.6% > pure positive | L5 emits mixed signal | HIGH |
| 6 | ARISE / Trajectory Analysis | ASE 2025 | Anti-patterns: repeated actions, overfitting | Generic event taxonomy | HIGH |
| 7 | Hashimoto Harness Engineering | Feb 2026 | 52.8%->66.5% from harness alone | L5 IS harness engineering | HIGH |
| 8 | SWE-Search (Antoniades et al.) | ICLR 2025 | Hybrid value function, structural | L5 value = diff state + edit count | HIGH |
| 9 | Strands Agents (AWS) | 2025 | Steering hooks: 100% vs 82.5% | Fire at tool boundaries, not checkpoints | HIGH |
| 10 | Plan Compliance | arXiv 2026 | Plans lose salience | Re-inject at decision points | MEDIUM |
| 11 | JetBrains Complexity Trap | NeurIPS 2025 | Observation masking = summarization | Survive condensation, token cap | HIGH |
| 12 | LLMs Cannot Self-Correct (Huang et al.) | TACL 2024 | No self-correction without external feedback | L5 IS the external oracle | HIGH |

## Design Principles Derived

1. Event-driven, not timer-based (Strands, Hashimoto)
2. Generalized classifiers, not framework-specific (SWE-Pruner, ARISE)
3. Confidence gating prevents noise (FeedbackEval)
4. Append-only, no reset (SWE-agent ACI, OpenHands)
5. Structural witnesses from graph via L3/L3b, not L5 (RepoGraph, Agentless)
6. L5 is external oracle, not self-correction (Huang et al.)
7. Token-light emissions survive condensation (JetBrains)
