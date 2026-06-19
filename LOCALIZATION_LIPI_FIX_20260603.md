# LIPI — why GT localization ≈ grep (the "broken machine gun"), and the fixes

Date 2026-06-03. The whole-pipeline brief test (5 langs, semantic both halves, LSP,
fresh closures) gave **first@5 = 5/9** — strong on ts/go (2/2), weak on rust/py/js. The
machine gun (LSP-enriched graph) was getting ≈ grep accuracy. LIPI across all 4 avenues.

## Avenue 1 — LOGIC (the ranking)
The grep-spine + 3-way RRF architecture deliberately subordinates graph reach (grep is
the primary sort key; structure promotes only on verified issue-anchored ≤1-hop edges).
Per `LOCALIZATION_FINAL_REPORT.md:113-115`: gold is RECALLED but RANKED below hubs;
content (W_LEX) is under-weighted, reach over-promotes hubs. So a non-lexical buried gold
needs the content/semantic signal + hub demotion — the ranking logic is A factor, but not
the root (axum proved a cleaner graph alone didn't surface it). **Status: documented lever
(§3 of BRIEFING.md); not the primary bug.**

## Avenue 2 — IMPLEMENTATION (the root bug, FIXED)
**`resolve.py:_get_ambiguous_edges` had a hardcoded `ORDER BY e.confidence ASC LIMIT 500`.**
The LSP resolve could NEVER process more than 500 edges per run, regardless of
`--max-edges`. On graphs with thousands of name_match edges (axum 2213, boa 19293,
marimo 19395) only the lowest-confidence 500 were ever resolved → **the graph stayed
30-50% name_match noise forever.** The machine gun was physically capped at 500 rounds.
Measured: axum "full" resolve processed exactly 500 edges (21 verified + 171 corrected +
203 deleted + 105 failed = 500), leaving 1818 name_match unresolved.
**FIX (committed):** `_get_ambiguous_edges` takes `limit` (default 500), driven by the
caller's `--max-edges`; the SQL uses `LIMIT ?`. A full resolve now reaches ALL ambiguous
edges and can fully clean the structural graph. Verified: re-resolving axum uncapped
processes all 2213 (was 500).

## Avenue 3 — INTEGRATION
The resolved graph reaches the brief (the brief reads graph_db directly). The closure is
rebuilt after resolve (`gt-index -rebuild-closure`, commit 89615b60) so it reflects the
LSP-corrected edges. No integration break found. **Status: clean.**

## Avenue 4 — PLUMBING (graph.db columns → layers; gaps found)
| column/data | stored | read by | GAP |
|---|---|---|---|
| edges.confidence | ✓ | every traversal (gating) | no |
| edges.resolution_method | ✓ | every layer | no |
| **edges.trust_tier** | ✓ | post_edit only | **REDUNDANT (verified)** — trust_tier (CERTIFIED/CANDIDATE/SPECULATIVE) is the categorical form of confidence; confidence IS read by every traversal, so the signal reaches ranking. No wiring needed. |
| **edges.candidate_count** | ✓ | nobody | **REDUNDANT (verified)** — confidence is a direct function of candidate_count (1→0.9, 2→0.6, 3→0.4, 6→0.2). Confidence already encodes it and IS read by ranking. Benign dead column, not a gap. |
| nodes.signature/return_type | ✓ | contract_map (brief) | no |
| properties: return_shape/guard/boundary/exception/conditional | ✓ | contract_map (brief) | no |
| properties: param/field_read/class_field/caller_usage | ✓ | post_edit only (or dead caller_usage/docstring) | per-symbol evidence delivered post-edit, not in the localization brief — BY DESIGN, debatable |
| cochanges (table) | ✓ | post_edit reads table; **v1r_brief recomputes via git subprocess** | optimization lost; brief should read the cached table |
| closure | ✓ | graph.py only | not a ranking lever (measured no-op 3×) |

## Net diagnosis
The "machine gun broken" was REAL and multi-causal, primary = **Avenue 2: the LSP 500-cap**
(graph never fully cleaned → name_match-dominated → structural signal = noise → GT ≈ grep).
Secondary = the documented ranking lever (content>reach + hub demotion). Plumbing gaps
exist (trust_tier/candidate_count unread) but may be redundant with `confidence` — to verify.

## Fixes
1. **resolve.py LSP-cap** — parameterized LIMIT (COMMITTED 84aaf8a0). VERIFIED: uncapped
   axum resolve processed all 2213 (was 500) → lsp 192→497, 567 false positives deleted,
   name_match 35%→29%. BUT axum gold STILL ranks None on the cleaned graph → **graph
   cleanliness is NOT the bottleneck for buried golds; the RANKING is.** Also: LSP has a
   ceiling — 1230/2213 edges FAILED (rust-analyzer can't resolve external/cross-crate
   symbols), so even uncapped the graph stays ~29% name_match. The cap fix is necessary
   foundation (cleaner graph, fewer false edges) but NOT sufficient to surface golds.
2. **content>reach + hub demotion ranking lever (§3) — THE remaining lever.** Since a
   clean graph didn't surface the buried golds (axum proven), the documented ranking
   rebalance (W_REACH↓, W_LEX↑, hub demotion, min-candidate) is the only thing left that
   moves a recalled-but-rank-None gold into top-5. Measure one weight at a time on the
   guarded 5-lang harness.
3. trust_tier/candidate_count — VERIFIED redundant with confidence (which IS read by
   ranking). No wiring needed. Benign.
