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

## Step 2 — DONE (frontier-research-grounded)
Commits `da9431ec` (post_view dunder-only), `aeba9080` (graph_localizer sites A+B +
is_seed_pollutant homonym-axis correction), `8cc44877` (wrapper _BUILTIN_NOISE →
membership + data-derived homonym filter). Ran a 5-agent frontier-lab research pass
(Aider/RepoGraph/LocAgent/OrcaLoca/Agentless/Anthropic); its finding #1 (in-degree =
importance, not genericness) drove a correction to Step 1b's is_seed_pollutant →
HOMONYM+dunder only (holdout-verified: boilerplate is homonymous, unique-def hubs kept).
Axis-separation law recorded. Regression 310 passed / 26 skipped; Site C logic validated
on crossplane holdout (wrapper needs OH runtime to import → full integration on Codespaces).

## Next allowed action
Step 3 (the flip-relevant one): `rrf_fuse` + `dynamic_cutoff` into
`graph_localizer.localize` AND replace the uncited weighted-2.0 RRF in `v2_ranker.py`,
with a k-sensitivity sweep (k=60 is a long-list default — few-list washout risk). Then
the deferred witness-display symbol_specificity ordering (needs conn in render_witness).
Full-trajectory verification on **GitHub Codespaces** (not GHA) after the localizer wiring.
