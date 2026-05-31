# GroundTruth — Dynamic-Conformance Remediation Plan

**Goal:** make GT's gating **dynamic + hybrid + confidence-gated + generalized**
by replacing ~64 hardcoded "poison" gates with **5 centralized, research-backed,
data-derived primitives**, and fix the remaining plumbing/integration bugs. Then
the rule everywhere is "score by data-derived confidence, gate dynamically" — and
an invariant test stops a new list/float from shipping.

Status: **fully mapped + foundation built (`groundtruth/confidence.py`, committed
`ab87881f`). NOT YET WIRED.** This doc is the execution handoff.

---

## 1. The 5 primitives (built, single source = `src/groundtruth/confidence.py`)

| Primitive | Method | Cites |
|---|---|---|
| `symbol_specificity(name, conn)` | geomean(RSJ/BM25-IDF def-freq × in-degree hub-penalty[P95] × name-token IDF) | BugLocator ICSE'12, BLUiR ASE'13, Robertson&Zaragoza FnTIR'09, RepoGraph ICLR'25 |
| `dynamic_cutoff(scores)` | median+MAD modified-z + percentile knee, degenerate guards | Iglewicz-Hoaglin'93, Leys'13, Satopaa(Kneedle)'11 |
| `rrf_fuse(signal→values)` | Reciprocal Rank Fusion, scale-invariant, no weights | Cormack&Clarke SIGIR'09, Fox&Shaw'94 |
| `claim_confidence(score, pool)` | conformal/selective-prediction + abstain | Vovk'05, Geifman&El-Yaniv NeurIPS'17 |
| `phase_and_budget(i, max_iter)` | fractions of max_iter / context window | Zilberstein'96 |

Proven on the live matplotlib graph: `__init__`→0.0 (168 files, degree 1087 — from
DATA), `run`→0.79 (rare in matplotlib; the blocklist wrongly dropped it everywhere).

---

## 2. Poison inventory (~64 Python + 6 Go) → which primitive kills it

**Symbol/stopword blocklists → `symbol_specificity`** (delete the lists; keep only
dunder + a tiny true-NL-stopword set):
`_GENERIC_SYMBOLS`, `_BUILTIN_NOISE`, `_STOPWORDS`(~190), `_STDLIB_HEADS`,
`_STDLIB_ATTRS`, `_generic_anchor`, `_get_name_match_peers`, `_detect_scope` common-method set.

**Magic edge floors `0.5/0.7/0.9` → existing categorical `_edge_filter_for_db`**
(~15 sites): `EDGE_CONFIDENCE_FLOOR`, hub_penalty `0.7`, post_view `0.5/0.9`,
post_edit `0.5/0.7`, wrapper scope `0.7/0.9`, grep `0.9`, `min_confidence 0.7`,
`VERIFY_MIN_EDGE_CONFIDENCE`, contracts `0.8`, conventions `0.7`.

**Hub scales → `specificity` P95**: `_HUB_SCALE=50`, `HUB_SCALE`.

**Separation/gap thresholds → `dynamic_cutoff`** (~12): `gap 0.25`, `gap>0.3`,
`0.5*score`, `overlap>=2`, `complexity<=3`, `shared>=2`, `usage_count>10`,
`AMBIGUITY_MARGIN 0.12`, `len(shared)>=2`, risk gates `>0.5/>0.4`, `noise_floor 0.12`.

**Fixed weights → `rrf_fuse`**: `W_WITNESS/W_LEX/W_SUBJECT/W_DEGREE`, composite
`0.40/0.25/0.15/0.20`, `risk_scorer._WEIGHTS`, **Go assertion weights 4.0/3.0/2.0**.

**Action-count bands → `phase_and_budget`**: `EARLY_END=5/MID_END=10` (curation),
`_classify_agent_state` windows, rescue cadence, L5 scaffold bands, router
`late_band_ratio=0.75`, grep rate-limit, char budgets `2000/600`.

**Go indexer (ROOT — every downstream gate consumes these):**
- `name_match` confidence ladder `0.9/0.6/0.4/0.2` (`resolver.go`) → `symbol_specificity`
  feeding `claim_confidence` (confidence ∝ name multiplicity in THIS repo, not bins). **POISON.**
- assertion-resolution weights `4.0/3.0/2.0` → `rrf_fuse`. **POISON.**
- assertion threshold `3.5` (fake-dynamic staircase) → `dynamic_cutoff`. **POISON.**
- per-strategy provenance tiers `0.95/0.9/0.85`, EXTENDS/IMPLEMENTS `1.0/0.8`,
  closure `MinEdgeConfidence 0.5` → **DEFENSIBLE** (categorical provenance / cross-boundary contract; keep).

**Defensible (keep):** dunder shape `__x__`, `_DETERMINISTIC_METHODS` set, MAD const
1.4826 / z 3.5, context-window char caps, deterministic tie-breaks.

---

## 3. Ordered wiring sequence (each step red-before-green, smoke vs a frozen trajectory)

0. **Cache lifecycle:** `confidence._db_key` now keys on db path+mtime (done). Confirm wrapper calls `confidence.clear_cache()` at task start.
1. **Anchors single-source (do FIRST — upstream of everything):** delete the wrapper's host-side `extract_issue_anchors` block (`oh_gt_full_wrapper.py:6420-6435`) — it races/overwrites the in-container write. `v1r_brief` already extracts once + persists. Wire `symbol_specificity` into `anchors.py` to replace `_STOPWORDS`/`_looks_like_natural_word` (keep a tiny NL set); rely on the existing graph cross-check.
2. **`symbol_specificity` into the 3 generic-name sites:** `post_view._generic_anchor` (revert my list), `graph_localizer._is_generic_symbol` + `_STDLIB_ATTRS` (revert my lists), `_BUILTIN_NOISE`.
3. **`rrf_fuse` + `dynamic_cutoff` into `graph_localizer.localize`:** replace the weighted-sum (lines ~573-578) with RRF over the 4 component dicts; replace the gate block (~618-636) with `dynamic_cutoff` (`high`→[VERIFIED]-eligible, `mid`→[INFO], `low`→suppress).
4. **`claim_confidence` into the brief tier emission** ([VERIFIED]/[WARNING]/[INFO]) and post_edit caller-evidence; honor `abstain` by suppressing.
5. **`phase_and_budget`:** add `--max-iter`/`--action-count` to both hooks' argparse + pass from wrapper (`:1288`, `:1321`); replace `curation.EARLY_END/MID_END`, router `late_band_ratio`, char budgets.
6. **Go indexer:** replace the `name_match` ladder + assertion weights/threshold with the same primitives (Go-side ports), so the ROOT confidences are data-derived.
7. **Invariant test:** property test that fails if any module defines a new symbol frozenset used for gating OR a new magic float compared against `e.confidence`/a score; plus an import-presence test that each consumer imports the primitive it should.

**Do NOT batch steps 3-5; wire one consumer, smoke against a captured trajectory, then the next.**

---

## 4. Remaining non-hardcoding bugs (7)

1. **Anchor single-source split on failure path** — delete the wrapper extract block (item in §3.1). *open.*
2. **Dead `[CANDIDATE]` path** (`post_view.py:709-711` + loader + `mirror_brief_candidates_to_tmp`) — remove fully (G6: never written). *open.*
3. **`confidence.py` unwired** — §3 is the fix. *open.*
4. **`_generic_anchor` poison (mine, post_view)** — revert to dunder-only + `symbol_specificity`. *open.*
5. **`_STDLIB_ATTRS`/`_GENERIC_SYMBOLS` poison** — replace with `symbol_specificity` < `dynamic_cutoff`. *open.*
6. **Path-form fragility** — route every path compare through the canonical normalizer before SQL bind/`endswith`. *open.*
7. **Rescue payload semi-prescriptive** (`_build_rescue_payload` L1-2) — de-prescribe like the brief (C2). *partially-fixed.*

Plus the earlier flow risks: anchor mismatch (half-fixed via §3.1), state path-form (#6).

---

## 5. What's already done this session (committed)

15+ fixes: categorical-filter in localizer BFS (`b481958d`), brief honors localize
rank (`c95c4f72`), meaningful witness (`0dde7db1`), DEFINES non-generic-only
(`b6b423f9`), 10 within-layer conformance fixes (`7f79ee5e`), consensus over-fire
parse (`e21a74fa`), the confidence-primitive foundation (`ab87881f`), +
`ARCHITECTURE_CONFORMANCE.md`. Live-proven on 4 unseen repos (beets/loguru/
geopandas/matplotlib): gold localized + edited, consensus delivered. **Caveat:**
geopandas/matplotlib unresolved (agent localized but wrote failing fixes —
localization is not the resolution bottleneck) and the matplotlib trajectory still
shows the contract-pillar first-3 until §3.2 is wired.

**The single test for done:** a fresh unknown-task trajectory clean of laundering /
wrong-function context / static-list suppression, with every gate data-derived.

---

## 6. Execution log — 2026-05-31 (wiring pass 1)

Research-validated the 5 primitives first (5 parallel agents + web verification).
**The validation changed the plan** — it found my "foundation" cited research it
does not use:

| Primitive | Verdict | Action taken |
|---|---|---|
| symbol_specificity | GO (citations overstated) | S1 re-labelled GT adaptation; RepoGraph moved to S2; "~0 EPS-floored" |
| dynamic_cutoff | GO + **integrity flag** | deleted the false Kneedle/Satopaa citation (no knee code existed); even-n MAD fixed to a true median; Leys=2.5 noted |
| rrf_fuse (confidence.py) | GO | confidence.py's is unweighted/honest; the **weighted-2.0 poison is in `v2_ranker.py`** (Step 3); k=60 few-list washout caveat noted |
| claim_confidence | **NO-GO as "conformal"** | dropped the Vovk p-value + target-risk claims; abstain now DELEGATES to dynamic_cutoff 'low' tier (one MAD authority) |
| phase_and_budget | GO (citation stretched) | unknown max_iter no longer silently → 100 (returns progress=-1.0); re-cited Russell&Zilberstein 1991 + BATS; taper marked provisional |

### Steps DONE (committed on gt-consensus-curation)
- **Step 0.5 (NEW — inserted before wiring)** `5309bbea` — the corrections above, in
  confidence.py. Red-before-green in `tests/test_confidence_primitives.py` (11→14
  cases). symbol_specificity byte-identical on holdout; no non-test consumer existed.
- **Step 1 / 1a** `9e13eef4` — single-source anchors: deleted the wrapper host-side
  `extract_issue_anchors` block; in-container v1r write is the sole source.
- **Step 1 / 1b** `23a1f0bf` — `is_seed_pollutant` (confidence.py) replaces the 190-word
  `_STOPWORDS` + `_looks_like_natural_word` symbol blocklist in anchor extraction.
  HUB (in-degree ≥ repo P95) OR HOMONYM (def-count > repo P95), ≥20-sample reliability
  guard, confidence-consistent P95 (latent S2 bug fixed). Broad list RETAINED for the
  lexical-query consumers (query_preprocessor/query_augment — legit IR stopwording).
  Verified on crossplane + axum holdouts; pretask 186 + layers 113 green.

### Step 0 (cache lifecycle) — re-assessed
`_db_key` keys on db path+mtime+size, so cross-task staleness is already prevented;
a wrapper `clear_cache()` at task start is now belt-and-suspenders, OPTIONAL.

### Step 2 DONE (committed) — grounded in a frontier-lab research pass (Aider repo-map,
RepoGraph ICLR'25, LocAgent ACL'25, OrcaLoca ICML'25, Agentless FSE'25, Anthropic 2025)
- `da9431ec` site 1 — `post_view._generic_anchor` → dunder-only (stop re-dropping `run`).
- `aeba9080` sites A+B — `graph_localizer`: `_is_generic_symbol` → dunder-shape only
  (dropped setUp/tearDown literals + dead `_GENERIC_SYMBOLS`); DEFINES-verify →
  `not is_seed_pollutant`; deleted dead `_STDLIB_HEADS`; `_STDLIB_ATTRS` documented as a
  Python-only band-aid deferred to Step 6 (import-scope).
- `8cc44877` site C — wrapper `_BUILTIN_NOISE` deleted; membership intersection handles
  English words, a data-derived in-container HOMONYM filter (repo P95 def-count) handles
  the hub half.
- **RESEARCH-DRIVEN CORRECTION to Step 1b:** finding #1 (Aider production) — in-degree
  measures IMPORTANCE, not genericness. `is_seed_pollutant` is now **HOMONYM (def-count
  > P95) + dunder ONLY**; the in-degree HUB component was removed (it caused an observed
  over-demotion of a uniquely-defined hub's DEFINES). Holdout-verified: all generic
  boilerplate is homonymous (crossplane Run=27/DeepCopyInto=11/New=7 files); unique-def
  hubs kept and merely deprioritized by symbol_specificity's soft S2 in ranking.
- **Axis-separation law (load-bearing):** RANKING→`symbol_specificity`;
  TRUST/SEED→`is_seed_pollutant` (homonym); MEMBERSHIP→`resolution_method` provenance.
- Open follow-up: full `symbol_specificity` witness-display ordering needs a conn
  threaded into the frozen `Witness.render_witness` (currently dunder-shape only).

### Steps REMAINING
3. rrf_fuse + dynamic_cutoff into `graph_localizer.localize` **and** replace the
   **weighted-2.0 RRF in `v2_ranker.py`** (uncited magic weights) — run a k-sensitivity
   sweep on real pool sizes (k=60 is a long-list default; few-list washout risk).
4. claim_confidence (now ECDF+delegated-abstain) into the brief [VERIFIED]/[WARNING]/
   [INFO] tiers + post_edit; OR drive tiers straight from dynamic_cutoff.
5. phase_and_budget — thread --max-iter/--action-count from the wrapper into both hooks.
6. Go indexer name_match ladder + assertion weights/threshold.
7. invariant test (no new gating frozenset / magic float vs a score).
