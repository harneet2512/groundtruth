# HARDCODED_NUMBERS.md — magic-number / threshold inventory

Audit of GT producer + integration code for hardcoded numeric constants, per the
**"Dynamic" mandatory property** (CLAUDE.md / DOC_OF_HONOR: tier boundaries must be
derived from per-task score distributions, not hardcoded absolutes).

Line numbers are from the files actually read this pass: `v1r_brief.py` (full),
`post_view.py` (full), `post_edit.py:1-1346`, `oh_gt_full_wrapper.py:1-2145 + 470-1346`.
Constants in unread regions are marked **(per DOC — verify line)**.

Classification:
- **DYNAMIC** — derived from per-task data (density, percentile, median). Compliant.
- **CITED** — fixed constant with a research/standard justification in the docs.
- **MAGIC** — hardcoded absolute, weak or no citation → candidate Dynamic-property concern.

---

## A. Edge-confidence / provenance thresholds (highest-signal for the Dynamic property)

| Value | Location | Gates | Class | Note |
|---|---|---|---|---|
| `EDGE_CONFIDENCE_FLOOR = 0.7` | v1r_brief.py:45 (used 161,211,247,286,323,330,432,1212,1432) | L1 function/test/callee/neighbor edge queries | **MAGIC** | flat 0.7 across all L1 graph queries; not per-task |
| `CALLER_CONFIDENCE_HI = 0.9`, `CALLER_CONFIDENCE_LO = 0.7` | v1r_brief.py:348-349 | retained, NO LONGER the gate (provenance is) | CITED | doc says superseded by categorical provenance; dead constants |
| `_NAME_MATCH_FLOOR` (0.5) | curation_map (imported v1r_brief.py:23) | name_match suppress<0.5 / `(unverified)`>=0.5 | CITED | The Distracting Effect 2505.06914 (plausible-wrong → never a fact) |
| categorical fallback `min_conf = 0.6` | post_edit.py:183 (`_legacy_confidence_filter_clause`) | L3 numeric fallback when post-merge schema absent | CITED | ICSE 2022 call-graph precision @0.6 |
| `COALESCE(e.confidence, 0.5)` | post_edit.py:396; post_view.py:40,462,776,997; wrapper grep | unknown-confidence default | CITED | Avro/Protobuf convention (DOC 6.2) |
| L3b caller/callee = categorical `_edge_filter` | post_view.py:589,597,630,719 | L3b primary edges | DYNAMIC | migrated to categorical |
| L3b importers `>= 0.5` | post_view.py:776 | importer edges | **MAGIC** | NOT migrated to categorical (partial-migration finding) |
| L3b hub-stats `>= 0.7` | post_view.py:667 | `all_degrees` p90 computation | **MAGIC** | NOT migrated |
| L3b ego BFS `min_confidence = 0.9` | post_view.py:878 | ego-graph center/callers | **MAGIC** | NOT migrated; also Gate that made ego near-dead (we_did 2.3) |
| L3b test-targets `>= 0.5` | post_view.py:997 | `_test_file_targets` | **MAGIC** | NOT migrated |
| L3 connected/header `>= 0.7` | post_edit.py:396 | `_annotate_evidence_header` connected files | **MAGIC** | numeric, not categorical |
| L3 EXTENDS/IMPLEMENTS / peers `>= 0.5` | post_edit.py:1169,1223 | interface-peer detection | **MAGIC** | numeric |
| grep intercept `min_conf = 0.6` | wrapper:316,355,367,413 | grep caller trace | CITED | matches L3 0.6 |

> **Cross-cutting consequence:** DOC §"Layer 8" tabulates uniform numeric thresholds
> (0.6/0.7/0.5) as the design; `we_did.md` claims migration to categorical. **Reality is
> SPLIT** — L3 + L3b primary caller/callee are categorical; importers/hub-stats/ego/test/
> header subqueries remain numeric MAGIC. Neither the DOC nor we_did fully reflects this.

---

## B. Result caps / budgets

| Value | Location | Gates | Class | Note |
|---|---|---|---|---|
| `MAX_FILES = 5` | v1r_brief.py:42 | brief candidate count | **MAGIC** | fixed top-5 |
| `MAX_FUNCTIONS_PER_FILE = 3` | v1r_brief.py:43 | functions per file | **MAGIC** | |
| `MAX_BRIEF_TOKENS = 600` | v1r_brief.py:44 | brief token budget | CITED | Anthropic "smallest high-signal set" 2025 |
| `MAX_CALLERS_PER_FUNC = 2` (×4 over-fetch) | v1r_brief.py:350,448 | callers per function | **MAGIC** | |
| `_MAX_EVIDENCE_CHARS = 2000` (~500 tok) | post_edit.py:111 | L3 evidence budget | CITED | budget math DOC 6.3 (<1% of 100K ctx) |
| `_brief_max_tokens(max_tokens=500)` → ×4 chars | wrapper:877 | brief tail char-cap | CITED | same budget math |
| contract pillar `LIMIT 30`, render `<= 3` lines | post_view.py:63,144 | L3b [CONTRACT] | CITED | high signal density |
| ego render `max_tokens = 150` | post_view.py:878,893 | ego block size | **MAGIC** | |
| sibling body `start+12` lines | post_edit.py:1129 | sibling snippet | CITED | Program Slicing ICSE 2024 (delta sufficient) |
| caller code clip `150/[:147]`, pre-context `[:90]` | post_edit.py:444,912 | snippet truncation | CITED | Batch 7 format (90/120) |
| L3b neighbor clip `90`, spec `45/42` | post_view.py:736,979 | snippet truncation | CITED | Batch 7 |
| char_caps `{early:1000, mid:640, late:320, final:0}` | post_view.py:746 | L3b per-band caps (flag GT_L3B_PRIMARY_EDGE) | **MAGIC** | 4 fixed band caps |

---

## C. Iteration-band / trajectory thresholds

| Value | Location | Gates | Class | Note |
|---|---|---|---|---|
| iteration ratios `0.25 / 0.60 / 0.85` | post_view.py:556-571,749,756,769,843 | L3b decay bands (early/mid/late/final) | **MAGIC** | fixed fractions of max_iter |
| presubmit `>= 3 actions` since last source edit | wrapper:1099 | edit→review transition | **MAGIC** | fixed action gap |
| scaffold redirect `scaffold_creates >= 3` | wrapper:1835 | L5 scaffold advisory escalation | **MAGIC** | |
| edit-loop `count >= 3` | wrapper:1841 | L5 diff-not-converging advisory | **MAGIC** | |
| `ac > 0.3 * max_iter` (no edits) | wrapper:1653 | stuck signal | DYNAMIC | scales with max_iter (relative) |

---

## D. `_classify_agent_state` stuck-detection thresholds (wrapper:1610-1659)

All **MAGIC** (fixed absolutes governing the rescue governor):
| Value | Gates |
|---|---|
| `ac - last_edit < 15` | CONVERTED (recent edit) |
| `ac - last_test < 10` | CONVERTED (testing after edit) |
| `recent_reads[-10:]`, `unique/len > 0.6` | PRODUCTIVE_SILENT |
| `ac - last_gt > 20` | +1 stuck signal |
| `_search_count_since_edit > 10` | +1 stuck signal |
| `unique/len < 0.4` | +1 stuck (high repeat) |
| `ac > 20` with no reads | +1 stuck |
| `len(_test_actions) > 3` no edits | +1 stuck |
| `stuck_signals >= 3` | HARMFUL_SILENT → rescue |

> The ≥3-signal **count** is hybrid (multi-signal), but each individual cutoff is a
> hardcoded absolute. Stuck-compat history: `last 8` entries, `md5(raw[:8000])`,
> history cap `24` (wrapper:3010-3035, per DOC — verify line). All MAGIC.

---

## E. Localizer / ranker constants (v1r_brief.py)

| Value | Location | Gates | Class |
|---|---|---|---|
| sparse threshold `_edges_per_file < 2.0` → BM25-only `W_LEX 0.70, W_PATH 0.45` | 1186-1196 | sparse-graph weight switch | **MAGIC** (threshold) + CITED (weights research) |
| `k_anchor=3, k_sem_top=10, tau_anchor=0.20, max_depth=3` | 1210-1212 | v7.4 ranker + localize | **MAGIC** |
| `localize(top_k=8)` | 1355 | graph-witness candidates | **MAGIC** |
| adaptive-K `gaps[i] > median_gap * 2` | 1238 | candidate cutoff | DYNAMIC (median-based) |
| `min_k = min(5, max_files, len)` | 1232 | recall floor | CITED |
| path rescue `comps.path >= 0.5` | 1294 | path-match preservation | **MAGIC** |
| neighbor `score * 0.8`, cross-domain bridge `* 0.6` | 1462,1487 | expansion scoring | **MAGIC** |
| convergence `lex > 0.5 * score` (all top-5) | 689 | overconfident-convergence detect | **MAGIC** |
| hub demotion trigger `_indexed_file_count >= 50`; `p80 = sorted[int(len*0.8)]` | 1500,1511 | hub reorder | **MAGIC** (50 trigger) + DYNAMIC (p80) |

---

## F. DYNAMIC / data-derived constants (the COMPLIANT contrast — keep these)

| Mechanism | Location | Derivation |
|---|---|---|
| `max_items` 2/3/5 by density (`e/n >3`, `<1`) | wrapper:830-849 (`_compute_repo_scale`) | graph density |
| `_dynamic_limit` sparse ×2 / dense ×2//3 | wrapper:852-858 | repo_scale |
| hub_scale = p90 in-degree (`all_degrees[int(len*0.9)]`) | post_view.py:669 | per-graph percentile (fallback 50 MAGIC) |
| co-change `min_count = 1 if median<=1 else 2` | v1r_brief.py:664-666 | per-repo median |
| adaptive-K via `median_gap` | v1r_brief.py:1235-1238 | per-task score gaps |
| hub demotion p80 in-degree | v1r_brief.py:1511 | per-graph percentile |
| dynamic assertion threshold (1→2.0, 2-3→3.0, 4+→3.5) | main.go (Go, DOC 0.6) | candidate count |

---

## G. Summary verdict (Dynamic property)

- **The ranking/curation CORE is mostly DYNAMIC** where it matters most: density-scaled limits, percentile hub scales, median co-change/adaptive-K. Good.
- **But a long tail of MAGIC absolutes remains**, concentrated in: (1) the **non-migrated L3b numeric edge thresholds** (importers/hub-stats/ego/test = 0.5/0.7/0.9), (2) **iteration bands** (0.25/0.60/0.85) and **char caps**, (3) the **entire stuck-detection governor** (15/10/20/0.4/0.6/3-signal), (4) **localizer hyperparameters** (k_anchor 3, max_depth 3, tau 0.20, top_k 8, MAX_FILES 5).
- **Flat `EDGE_CONFIDENCE_FLOOR = 0.7`** across all L1 graph queries is the single most-reused MAGIC threshold and the clearest Dynamic-property tension in L1.
- These are **documented here as candidates**, not auto-flagged as bugs — many (budget chars, 0.6, COALESCE 0.5, sibling≥2) are CITED and defensible. The Dynamic-property *violations* to escalate are the ones gating **evidence inclusion** on a flat absolute with no per-task derivation (the L3b numeric tail + `EDGE_CONFIDENCE_FLOOR 0.7`).

**No code modified.** Inventory only — feeds the IMPLEMENTATION_MAP "Dynamic property" column and the final bug ledger (P2/P3 unless a flat threshold demonstrably drops evidence).
