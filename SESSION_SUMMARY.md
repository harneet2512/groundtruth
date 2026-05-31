# Session Summary

## Date / Time
2026-05-31

## Branch
gt-consensus-curation

## Commit
HEAD `23a1f0bf` (chain: `9e13eef4` → `5309bbea` → `23a1f0bf`, parent `a5667812`)

## Objective
Execute the REMEDIATION_PLAN wiring — replace hardcoded "poison" gates with the
centralized, research-backed confidence.py primitives — as a **research-based**
implementation: one consumer at a time, red-before-green, verified on real graphs.

## Files read (evidence)
- `confidence.py` (full API), `REMEDIATION_PLAN.md` §3, `pretask/specificity.py` shim
- `pretask/anchors.py` (full), `scripts/swebench/oh_gt_full_wrapper.py:6400-6520`,
  `pretask/v1r_brief.py:1318-1365`, `hooks/post_view.py:328-349`, `hooks/post_edit.py:110-170`
- `tests/pretask/test_anchors.py`, `tests/pretask/conftest.py` (tiny_graph_db = 5 nodes)

## Research checked (before building)
6-agent validation workflow (web-verified) on the 5 primitives drove real changes
(REMEDIATION_PLAN §6): dynamic_cutoff cited a Kneedle knee it does NOT implement
(removed); claim_confidence mis-cited as conformal + abstained at an arbitrary median
(NO-GO → consolidated into dynamic_cutoff); symbol_specificity S1 over-attributed to
BugLocator/BLUiR (re-labelled GT adaptation); phase_and_budget silently defaulted
unknown max_iter to 100 (fixed); the weighted-2.0 RRF poison lives in v2_ranker.py.

## Implementation changes
1. **1a** `9e13eef4` — deleted the wrapper host-side `extract_issue_anchors` block;
   in-container `generate_v1r_brief` write is the single anchor source.
2. **0.5** `5309bbea` — confidence.py research-honesty + correctness: false-citation
   removals; even-n MAD → true median; claim_confidence abstain delegates to
   dynamic_cutoff; phase unknown-horizon → progress=-1.0.
3. **1b** `23a1f0bf` — `is_seed_pollutant` replaces the 190-word `_STOPWORDS` +
   `_looks_like_natural_word` SYMBOL blocklist in anchor extraction (HUB ≥ repo P95
   OR HOMONYM > repo P95, ≥20-sample guard, confidence-consistent P95). Broad list
   retained for lexical-query consumers. Latent S2 P95-filter bug fixed.

## Metrics before / after
Foundation wiring, not an agent run. Primitive suite 11→14 cases (red-before-green);
pretask 186 passed / 1 skipped (baseline unchanged); layers 113 passed / 25 skipped.
symbol_specificity byte-identical on crossplane holdout before the S2 fix; intended
~±0.1 shift after (P95 now consistent).

## Tests / runs executed
- `tests/test_confidence_primitives.py` (14), `tests/test_anchors_specificity.py` (2)
- `tests/pretask` (186/1), `tests/layers` (113/25)
- Real-graph smokes: crossplane-7246 + axum-3661 holdout graphs (is_seed_pollutant +
  extract_issue_anchors end-to-end)

## Result
Steps 1a + 0.5 + 1b done and committed; foundation now research-honest and the anchor
path is data-derived. All gates green. No agent flip claim — this is pre-wiring; the
flip-relevant wiring (localizer rrf/cutoff, brief tiers) is Steps 3-4.

## Regressions
None. Two pretask tests broke transiently during 1b (tiny-graph P95 misfire + an NL
contract test asserting the OLD poison) — both root-caused: added the ≥20-sample
reliability guard, and updated the NL contract test to the new (correct) behavior.

## Rollback decision
Each step is an isolated commit. Revert `23a1f0bf` / `5309bbea` / `9e13eef4`
independently. No push yet.

## Open blockers
None. confidence.py primitives now correct; consumers 2-7 remain unwired.

## Next allowed action
Step 2: wire `is_seed_pollutant` / `symbol_specificity` into the 3 generic-name sites
(`post_view._generic_anchor`, `graph_localizer._is_generic_symbol`+`_STDLIB_ATTRS`,
wrapper `_BUILTIN_NOISE`), red-before-green; then Step 3 (localizer rrf/cutoff +
v2_ranker weighted-RRF poison + k-sweep). Full-trajectory verification on **GitHub
Codespaces** (not GHA) after the localizer wiring.
