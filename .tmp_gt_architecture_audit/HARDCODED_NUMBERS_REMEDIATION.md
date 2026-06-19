# HARDCODED_NUMBERS_REMEDIATION.md — before/after, research-backed, additive

Closes the `HARDCODED_NUMBERS.md` finding per the directive: *replace hardcoded numbers
with dynamic + hybrid + confidence-gated mechanisms "as others", frontier-research-backed,
documented before/after, **do not delete** (additive only), other bugs → next session.*

## Governing principles (so this stays honest)

1. **"As others" = reuse the EXISTING cited mechanism, not a new invented one.** The
   in-repo dynamic+hybrid+confidence-gated primitive is `post_edit._edge_filter_for_db`
   (categorical: `resolution_method` ∈ strong-set **OR** `name_match & candidate_count≤1`
   **OR** `trust_tier ∈ {CERTIFIED,CANDIDATE}`, **AND** not `SUPPRESSED`; numeric fallback
   on old schema). Research already on record: **PyCG ICSE 2021** (structural resolution
   methods are the trustworthy signal), **Anthropic "Writing Effective Tools" 2025** (filter
   hard upstream, verbatim downstream), **Squeez arXiv 2604.04979 2026** (aggressive
   pre-display filtering). Plus density/percentile scaling already live (`_compute_repo_scale`,
   p90 hub_scale, median adaptive-K).
2. **Additive — nothing deleted.** Numeric constants are RETAINED as the `min_conf` fallback
   passed into the categorical helper, so behavior on an old-schema graph.db is **byte-identical**
   to before; the categorical path only engages when the post-merge columns exist.
3. **No invented thresholds or citations.** Where a number has no cited dynamic analog
   (iteration bands, stuck cutoffs, localizer hyperparams), it is **DEFER**, not "dynamized."
4. **KEEP-CITED stays.** A token/char BUDGET is not a confidence tier — making it "dynamic"
   has no research basis and risks context bloat (Du et al. EMNLP 2025; ETH AGENTS.md 2026).
   Document why it stays; do not churn it.

---

## TABLE 1 — REPLACE-NOW (clean files only: `v1r_brief.py`, `post_view.py`)

Each: flat numeric edge-confidence gate → categorical `_edge_filter_for_db(min_conf=<old value>)`.
Additive (old value retained as fallback). "As others" = same primitive L3/L3b caller/callee already use.

| # | BEFORE (file:line) | BEFORE value | AFTER (mechanism) | Behavior delta | Risk |
|---|---|---|---|---|---|
| 1 | `v1r_brief.py:45` `EDGE_CONFIDENCE_FLOOR=0.7` used at 161/211/247/286/323/330/1432 | flat `AND e.confidence >= 0.7` | `AND {_edge_filter_for_db(graph_db, min_conf=0.7)}` | old schema: identical; new schema: categorical (verified-edge ranking, trust-tier aware) | LOW (additive, proven pattern); shifts ref-count ranking toward verified edges — **needs live confirm (Axis 2)** |
| 2 | `post_view.py:997` `_test_file_targets` | `COALESCE(e.confidence,0.5) >= 0.5` (CALLS test→src) | `_edge_filter(db_path)` | new schema: verified test-call edges only | LOW (CALLS edges; same primitive) |
| 3 | `post_view.py:667` hub `all_degrees` stat | `COALESCE(e.confidence,0.5) >= 0.7` | `_edge_filter(db_path)` | hub_scale p90 computed over verified edges | LOW (statistic only; affects demotion scale slightly) |

> **Implementation sketch (#1), additive:**
> ```python
> # v1r_brief.py — replace the conf_clause builder
> from groundtruth.hooks.post_edit import _edge_filter_for_db  # the cited primitive
> def _conf_clause(graph_db, alias="e"):
>     # categorical when post-merge schema present; else numeric >=0.7 (UNCHANGED fallback)
>     return "AND " + _edge_filter_for_db(graph_db, alias=alias, min_conf=EDGE_CONFIDENCE_FLOOR)
> ```
> EDGE_CONFIDENCE_FLOOR is **kept** as the fallback constant (not deleted).

---

## TABLE 2 — KEEP-CITED (documented rationale, NOT changed)

| Number | Location | Why it stays |
|---|---|---|
| `_MAX_EVIDENCE_CHARS=2000` / `MAX_BRIEF_TOKENS=600` / `_brief_max_tokens=500` | post_edit.py:111; v1r_brief.py:44; wrapper:877 | BUDGET, not a tier. Dynamizing a budget risks bloat (Du EMNLP 2025; ETH AGENTS.md 2026; budget math DOC 6.3). |
| `min_conf=0.6` (L3 numeric fallback) | post_edit.py:183 | CITED ICSE 2022 call-graph precision; it is already only the *fallback* under the categorical helper. |
| `COALESCE(..,0.5)` default | many | CITED Avro/Protobuf unknown-confidence convention. |
| ego `min_confidence=0.9` | post_view.py:878 | **DELIBERATE** rare-but-honest gate (we_did.md §2.3 C-dropped: relaxing it re-introduces "confident on weak signals"). Lowering = regression. KEEP. |
| importers `>= 0.5` | post_view.py:776 | IMPORTS edges have different provenance than CALLS; the CALLS-oriented categorical strong-set would wrongly suppress valid imports. KEEP numeric (or design an IMPORTS-specific categorical next session). |
| sibling gate `len>=2`, co-change `>=2/3`, twins `2..6`, body `12 lines` | post_edit.py | CITED DevReplay 2020 / Program Slicing ICSE 2024 — frequency/locality constants, not confidence tiers. |

---

## TABLE 3 — DEFER → next session (dirty files OR needs validation/research)

| Number(s) | Location | Why deferred |
|---|---|---|
| L3b iteration bands `0.25/0.60/0.85`, char_caps `{1000,640,320,0}` | post_view.py:556-571,746 | No cited dynamic analog; behavior-shaping; flag-gated (`GT_REBUILD_L3B`). Needs design + live validation. |
| Entire `_classify_agent_state` governor (`15/10/20/0.4/0.6/3-signal`) | **oh_gt_full_wrapper.py:1610-1659 (DIRTY FILE)** | Stacking risk + governs rescue behavior; needs live validation. |
| Localizer hyperparams `k_anchor=3, k_sem_top=10, tau_anchor=0.20, max_depth=3, top_k=8, MAX_FILES=5` | v1r_brief.py:1210-1212,1355,42 | Algorithm hyperparameters (BFS depth, candidate K) — dynamizing is a research task (adaptive-depth retrieval), not a threshold swap. Direct Axis-2 efficacy levers → tune via live run, not blind. |
| post_edit numeric subqueries (`0.6/0.5/0.7` at 396/1169/1223) | **post_edit.py (DIRTY FILE)** | Stacking risk; migrate to categorical next session once dirty tree is resolved. |
| sparse threshold `_edges_per_file < 2.0`, hub trigger `>=50`, convergence `lex>0.5` | v1r_brief.py:1186,1500,689 | Behavior-shaping ranker constants; want live validation before changing. |

---

## Deferred OTHER bugs → next session (per directive; NOT resolved here, NOT deleted)

| Bug | Locus | Why next session |
|---|---|---|
| `os.walk`→`account.walk` laundering (P0) | **Go resolver provenance** (gt-index) | Requires Go build (no Go/GCC locally — only prebuilt `gt-index-t1t2.exe`); CI-only fix. Secondary `_is_stdlib_shadow` guard already present in v1r. |
| `__GT_STRUCTURED__` stdout leak risk (P1) | L3b dispatch in **oh_gt_full_wrapper.py (DIRTY)** | Verify/repair the stdout split; dirty-file + needs runtime repro. |
| `v22_brief` dead path still present in committed `eaa45b9c` (P2) | `generate_task_brief` (**wrapper, DIRTY**) | The dirty working tree already removes it; confirm + land next session. |

---

## Compliance with the directive

- **Research-backed:** every REPLACE reuses an already-cited mechanism; no invented citations.
- **Dynamic + hybrid + confidence-gated "as others":** categorical `_edge_filter_for_db` is exactly the L3/L3b primitive.
- **Before/after documented:** Tables 1–3 above.
- **Do not delete:** all changes additive; numeric constants retained as fallbacks.
- **Other bugs → next session:** listed above, untouched.

---

## APPLIED THIS SESSION (before → after, real diffs)

**v1r_brief.py — flagship `EDGE_CONFIDENCE_FLOOR` (most-reused MAGIC threshold) → categorical, additive.**
- ADDED `_edge_conf_clause(graph_db, alias="e")` helper (after `_has_confidence`): returns `""` when no
  confidence column (unchanged), categorical `_edge_filter_for_db` clause on post-merge schema, numeric
  `confidence >= EDGE_CONFIDENCE_FLOOR` fallback on old schema. `EDGE_CONFIDENCE_FLOOR` constant RETAINED.
- BEFORE (×6 sites): `conf_clause = f"AND e.confidence >= {EDGE_CONFIDENCE_FLOOR}" if _has_confidence(graph_db) else ""`
- AFTER (×6 sites): `conf_clause = _edge_conf_clause(graph_db)`  (5 via replace_all + the neighbor-expansion site)

**post_view.py — `_test_file_targets` test-link gate → categorical, additive.**
- BEFORE: `AND COALESCE(e.confidence, 0.5) >= 0.5`
- AFTER: `AND {_edge_filter(db_path)}` (query made an f-string; reuses the existing L3b categorical helper)

**Verification:** import smoke OK; helper returns `''` on no-confidence DB (additive no-gate confirmed);
**99 passed / 0 failed** focused suite (test_v1r_brief, test_v1r_brief_tiers, test_post_view_contract_pillar,
test_post_edit_categorical_filter, test_v7_4_brief, test_l4a_categorical). No new diagnostics at edit sites
(the surfaced `graph_db unused` @694 and post_view `str|None`/unused-import warnings are PRE-EXISTING, not from this change).

**NOT applied (kept clean, per "don't muddy waters"):**
- post_view hub-stats (667) — only a percentile STATISTIC, not an evidence gate → KEEP (no correctness gain, would churn the diff).
- All Table-3 + dirty-file (post_edit.py, oh_gt_full_wrapper.py) numbers + OTHER bugs → next session.

**Status:** Table-1 clean-file remediation APPLIED + unit-green. Per DONE=metrics this is "in progress"
until a next-session LIVE run confirms no Axis-2 (efficacy) regression. Additive, fully revertable
(`git revert` the code commit) — old-schema behavior is byte-identical.
