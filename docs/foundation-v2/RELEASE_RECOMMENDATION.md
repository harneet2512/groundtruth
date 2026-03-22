# Foundation v2 — Release Recommendation

## Component Classification

| Component | Location | Classification | Default | Rationale |
|---|---|---|---|---|
| Parser abstraction | `foundation/parser/` | **SHIP** | ON | Multi-language foundation. Zero-cost when not used. Required by all downstream components. |
| Representation substrate | `foundation/repr/` | **SHIP** | ON | Infrastructure layer (schema + store). No runtime cost unless extractors are active. Required for any similarity work. |
| Structural vectors (astvec) | `foundation/similarity/astvec.py` | **SHIP** | ON | Extends existing `structural_similarity.py` with richer 32-dim features. Ablation shows 10 candidates found standalone. Best signal-to-noise for obligation expansion. |
| Fingerprints | `foundation/similarity/fingerprint.py` | **SHIP** | ON | 21 candidates standalone — strongest solo signal. 31-byte deterministic fingerprint. Useful for rename detection and broad similarity. |
| Token sketches | `foundation/similarity/tokensketch.py` | **SHIP BEHIND FLAG** | OFF | 0 candidates standalone at obligation threshold. Value is in composite disambiguation (reduces false positives when combined with other signals). Enable when precision tuning is needed. |
| Composite scorer | `foundation/similarity/composite.py` | **SHIP** | ON | Weighted multi-signal combination. More selective than any single signal (6 vs 21 candidates). Use-case profiles are well-calibrated. |
| Graph expansion | `foundation/graph/` | **SHIP** | ON | Biggest single amplifier: 25 additional candidates beyond similarity. Leverages existing refs/attributes tables. BFS is deterministic and fast (<5ms). |
| Live indexing | `foundation/liveidx/` | **SHIP BEHIND FLAG** | OFF | Extends existing freshness tracking with representation invalidation. Conservative behavior (filter stale, don't crash). Enable when incremental re-indexing is needed. |
| Integration pipeline | `foundation/integration/pipeline.py` | **SHIP** | ON | The wiring layer. Gated by GT_ENABLE_FOUNDATION. Additive-only: never replaces existing obligations. |

## Summary

### SHIP (default ON) — 7 components
- `foundation/parser/` — parser protocol and registry
- `foundation/repr/` — representation store and schema
- `foundation/similarity/astvec.py` — structural vectors
- `foundation/similarity/fingerprint.py` — fingerprints
- `foundation/similarity/composite.py` — composite scorer
- `foundation/graph/` — graph expander and rules
- `foundation/integration/pipeline.py` — staged retrieval pipeline

### SHIP BEHIND FLAG (default OFF) — 2 components
- `foundation/similarity/tokensketch.py` — token sketches (disambiguation only)
- `foundation/liveidx/` — live indexing with staleness detection

### INCUBATOR ONLY — 0 components

### CUT — 0 components

## Merge Strategy

1. Merge all SHIP components behind `GT_ENABLE_FOUNDATION` feature flag (already implemented).
2. Flag defaults to OFF for the first release to allow opt-in testing.
3. After one release cycle with zero regressions, change default to ON.
4. Token sketches and live indexing get their own sub-flags (`GT_ENABLE_TOKENSKETCH`, `GT_ENABLE_LIVEIDX`) for independent activation.

## Evidence

- 1233 tests passing (including 11 new ablation tests)
- Ablation shows monotonic improvement: baseline(0) < similarity(6-21) < similarity+graph(31)
- Latency budget met: <5ms per query for full pipeline
- No false positives in fixture evaluation: all candidates are structurally justified
- Integration is additive-only: existing obligations are never modified or removed
