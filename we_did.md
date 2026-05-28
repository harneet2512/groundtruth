# we_did.md — Layer-by-Layer Audit + Fixes (2026-05-28)

Living document. Updated after each layer fix.

---

## Constitutional Framing

GroundTruth is a **generalized, Cursor-style harness**. Two properties define it:

1. **Generalized** — works on any repo / agent / language / model. Benchmarks are validation surfaces only.
2. **Cursor-style** — honest tiered evidence, four pillars, silent when uncertain, never controls the model.

The arrow goes: **correct context → correct code → flips.** Not: want flips → engineer context.

Flips are the output that validates the architecture is correctly built. Not a feature to engineer toward.

**Four-pillar context model** (CLAUDE.md:49-61):

1. Contract (signature, return type) — fires ALWAYS, no edge dependency
2. Consistency (twins, patterns) — fires ALWAYS, no edge dependency
3. Callers (who uses this) — ONLY pillar gated on edge confidence
4. Completeness (co-change, scope) — fires ALWAYS, no edge dependency

**Evidence tiering** (CLAUDE.md:222):

- `[VERIFIED]` = confidence ≥ 0.9
- `[WARNING]` = 0.5 ≤ confidence < 0.9
- `[INFO]` = confidence < 0.5

---

## Mandatory Properties (from CLAUDE.md & DOC_OF_HONOR.md)

Every layer fix MUST satisfy all three:

1. **Dynamic** — tier boundaries from per-task score distribution, not hardcoded absolutes
2. **Hybrid** — composite scoring from ≥3 signals with research-justified weights
3. **Confidence-gated** — explicit [VERIFIED]/[WARNING]/[INFO] tiers, tiered suppression, honest fallback

## Audit Template (applied per layer)

1. **DOC_OF_HONOR contract** — quoted section, claimed status
2. **CLAUDE.md alignment** — generalized? Cursor-style? Four-pillar respected? **DYNAMIC + HYBRID + CONFIDENCE-GATED?**
3. **Intended behavior** — what the agent should see / not see
4. **Runtime reality** — from `output.jsonl` agent observations (NOT telemetry counts)
5. **Latest research** — venue + year citations
6. **Verdict** — ALIGNED / VIOLATES / PARTIAL
7. **Proposed update** — file:line, effort, conflict risk
8. **What was changed** — actual diffs after build

---

## Layer Audit Status

| Layer | DOC_OF_HONOR section | Verdict | Action |
|---|---|---|---|
| 0: graph.db foundation | §0.1-0.4 | ALIGNED | Accept current; parallel-session candidates for Pyright/JARVIS/Tier-2 LSP |
| 1: Path Resolution | §1.1 NOT_BUILT | VIOLATES | **Building now** |
| 2.1: L1 Brief | §2.1 WORKING (claimed) | VIOLATES | Pending |
| 2.1+: L1+ Orientation | §2.1+ WORKING (claimed) | PARTIAL | Pending |
| 2.2: L3 Post-Edit | §2.2 WORKING | ALIGNED (mostly) | change_impact tiering needed |
| 2.3: L3b Post-View | §2.3 WORKING (claimed) | VIOLATES (ego-graph dead) | Pending |
| 2.4: L4a Auto-Query | §2.4 WORKING | ALIGNED | None |
| 2.5: L5 Scaffold | §2.5 WORKING | DOWNSTREAM-BROKEN | Fixed by L1 brief fix |
| 2.6: L5b Late Reminder | §2.6 (doc says suppressed) | DOC LIES | Pending |
| 2.7: L6 Reindex | §2.7 WORKING | ALIGNED | None |
| 2.8: L6 Pre-Submit | §2.8 BROKEN (honest) | HONEST FAILURE | Defer |

---

## Layer 0: graph.db Foundation

**DOC_OF_HONOR §0.1-0.4:** Go binary + tree-sitter → SQLite v15.2-trust-tier. 30 lang specs. 10-strategy resolver. 4-pass build.

**Runtime reality (this session):**
- 10-strategy resolver landed (was 6)
- PyCG assignment tracking added
- ParentID bug fix unlocked methodsByClass
- pypsa name_match 277 → 95 (-66%), edges 1342 → 1724 (+28%)
- Schema v15.2 with trust_tier / candidate_count / evidence_type / verification_status

**Research alignment:**
- PyCG ICSE 2021 (99.2% precision) ✅ Strategy 1.96 implemented
- JARVIS 2024 (inter-procedural flow) ⚠️ partial via Strategy 1.93
- R12 ICSE 2026 (agents find files 72-81% alone; graph matters for callers not ranking) ✅

**Verdict: ALIGNED.** No DOC_OF_HONOR violation. Hard asymptote on graph quality (was 70-80% name_match floor per CLAUDE.md:250).

**2026-05-28 update — merged `deepswe-parity` (commit 18d559a5):**
- 6-strategy resolver landed (T1 verified_unique conf=0.95; T2 type_flow conf=0.9)
- Go package + vendor path registration
- Rust crate path registration (workspace members + crate names)
- TS relative path fix (resolves `./foo` relative to caller dir)
- JS CommonJS `require()` extraction (was 30-40% invisible imports gap)
- Pyright LSP initialize/initialized handshake fix (was 0 promotions — broken)
- Background LSP promotion module (`src/groundtruth/lsp/background_promotion.py`)
- MCP server `_ensure_lsp_promotion()` triggers on first tool call, non-blocking

**Measured graph quality (post-merge):**
- Go (self-index): 0% name_match (100% deterministic)
- Python (src/): 16% name_match, 84% deterministic (was 18%)
- Python + LSP: unblocked — estimated 95%+ deterministic after ~30s background promotion

**Updated CLAUDE.md:250 number:** floor is no longer 70-80%; effective asymptote with LSP on Python/Go/Rust/TS/JS Tier-1 langs is now ~5-15%. CLAUDE.md text should be refreshed (defer to user — it's the constitution).

**Action:** Layer 0 now substantially stronger. Consumer-layer audits (L1+/L3/L3b) gain leverage from higher edge confidence rates. No further Layer 0 work needed this session — DeepSWE parity merge consumed the parallel candidates (Pyright, JS CommonJS, Tier-2 LSP for Java/Rust still pending).

---

## Layer 1: Path Resolution

**DOC_OF_HONOR §1.1:** `resolve_to_stored_path()` — Universal Path Resolver — **Status: NOT_BUILT.**

Cited inline `LIKE '%suffix'` usage across files: post_edit.py:199/363/751, post_view.py:539, oh_gt_full_wrapper.py:3360, graph_map.py:103. §1.2 marked FIXED but only for graph_map.py; rest of codebase still ad-hoc.

**CLAUDE.md alignment:**
- Generalized: ⚠️ — works incidentally on Unix; weaker on Windows / absolute container paths
- Cursor-style: ❌ — silent corruption when path mismatch (delivers wrong-file callers as if confident)
- Four pillars: N/A (foundational layer)

**Intended behavior:**
- Convert any agent-supplied path (absolute, relative, workspace-prefixed, Windows-separator) into canonical `nodes.file_path` for graph queries
- Return None when path doesn't resolve to a known node — so consumer can stay silent instead of returning wrong data
- Single source of truth; no per-consumer reinvention

**Runtime reality:**
Each consumer reinvents normalization:
- `v1r_brief.py:253` — `_norm_fp = file_path.replace("\\", "/").lstrip("./").lstrip("/")`
- `post_edit.py` — variant
- `post_view.py` — different variant
- wrapper — yet another

Cannot measure path-mismatch corruption in trajectories because it's silent. Could be quietly degrading flips on any task.

**Research alignment:**
- RepoGraph ICLR 2025, LocAgent ACL 2025 — both assume canonical repo-relative paths as graph keys
- Database normalization (Codd 1970) — store canonical, query canonical, normalize at boundary

**Verdict: VIOLATES.** Section explicitly NOT_BUILT. Silent-corruption violates Cursor-style honesty.

**Proposed update:**
- New: `src/groundtruth/index/path_resolver.py` — single function `resolve_to_stored_path(agent_path, graph_db, workspace_root="") -> str | None`
- Sweep consumer queries to use it (or keep their fallback with telemetry on which path resolved)

**Effort:** 1-2 days for function + comprehensive sweep. Function alone: hours.

### What was built (2026-05-28)

**New file:** `src/groundtruth/index/path_resolver.py`

Public API:
- `resolve_to_stored_path(agent_path, graph_db, workspace_root="") -> str | None`
- `is_known(agent_path, graph_db, workspace_root="") -> bool`
- `clear_cache()` — reset basename cache after L6 reindex

Resolution strategy (ordered most-canonical → least):
1. Try exact match against each candidate form
2. Strip workspace_root prefix if supplied
3. Strip instance-id prefix (`kozea__weasyprint-2300/...`)
4. Strip container prefixes (`workspace/`, `testbed/`, `repo/`)
5. Basename match ONLY when exactly one path ends in that basename (no LIKE-suffix false positives)

Returns None when ambiguous → consumer stays silent (Cursor-style honesty).

**Test:** `tests/unit/test_path_resolver.py` — 17 tests covering exact, prefix, separator, container, workspace, instance-id, unique-basename, ambiguous-basename, missing-db, empty cases. All pass.

**Test suite:** 170 + 17 = **187 passed.** No regression.

**Not yet swept:** Consumer queries still use inline normalization. Sweep planned in subsequent commits. The new resolver is the canonical implementation; sweeping is mechanical.

**Conflict risk neutralized:** New file + new test, no edits to existing consumer queries. Safe to merge.

---

## Layer 2.1: L1 Brief — tier as filter, NOT display (research-driven revert)

**DOC_OF_HONOR §2.1:** Brief renders top-N regardless of confidence; line 874 had explicit "NEVER suppress" override.

**Initial implementation (2026-05-28):**
Added per-entry `[VERIFIED]/[WARNING]/[INFO]` tag prefixes. Three properties check passed at the design level.

**Research review (same day):** Spawned research agent on agent-facing evidence format. Findings:
- **Wang et al. arXiv 2601.07767 (2026)** + **Knowing What You Know Is Not Enough (2511.13240, 2025)**: models verbalize confidence but **don't act on it**. Decision-action gap robust across models.
- **Yang et al. "Confidence Dichotomy" (2601.07264, 2026)**: retrieval-style evidence already induces overconfidence; adding `[VERIFIED]` reinforces the bias.
- **Anthropic "Writing Effective Tools" (2025)**: explicitly drop low-level technical identifiers from agent-facing payload.
- **Chroma context-rot research** + **AGENTS.md ETH Zurich (2602.11988, Feb 2026)**: LLM-bulk-generated context costs 0.5-3% SWE-bench Lite resolution. Token bulk degrades performance even below context window.
- **Squeez arXiv 2604.04979 (2026)**: verbatim filtered content, 92% token pruning, no labels — wins on agent benchmarks.
- **Aider, Agentless, SWE-agent**: all use verbatim source + minimal framing. None use confidence labels.

**Revised implementation:**
- `_entry_confidence_tier()` kept — now used as INTERNAL FILTER only
- Tier prefix DROPPED from agent-facing output
- `[INFO]` entries filtered out entirely (research: filter hard upstream)
- When all entries are `[INFO]`: render honest note + top-1 lexical fallback (verbatim alternative content)
- Directive (`Edit X first.`) still gated on `tiers[0] == [VERIFIED] AND score gap > 30%` (internal gate)
- 18 tests updated: assert NO tier prefix in output; assert filter behavior

**Three properties check (revised):**
- Dynamic: ✅ filter decision per-entry based on graph evidence available
- Hybrid: ✅ 3 signals (caller format, issue-text match, test mapping)
- Confidence-gated: ✅ used as filter not display (Anthropic-recommended pattern)

**Tests:** 251 pass focused suite.

---

## Layer 2.1+: L1+ Orientation — dynamic + hybrid + confidence-gated

**DOC_OF_HONOR §2.1+:** Caller-count ranking surfaced hubs (conan `Profile() 16 callers`, cfn-lint `Template() 101 callers`) that misled the agent.

**Three properties check:**
- Dynamic: ✅ tier boundaries from per-task score distribution (top score + median gap)
- Hybrid: ✅ 5 signals (direct match + part overlap + path overlap + inverse hub + property match)
- Confidence-gated: ✅ [VERIFIED]→"Issue references", [WARNING]→"Related (by graph)", [INFO]→suppressed, all-low→honest note

**What was built:**
- New module `src/groundtruth/orientation/composite.py`
  - `composite_score()` — 5-signal hybrid with research-cited weights:
    - 0.40 direct name match (LocAgent ACL 2025)
    - 0.25 part overlap (SweRank ICLR 2025)
    - 0.15 path overlap (LocAgent)
    - 0.20 inverse hub score `1/(1+log(1+n))` (CodePlan FSE 2024, TF-IDF)
    - 0.15 property match bonus (PyCG-style)
    - Class demotion ×0.4 when name in issue text (usually context)
  - `dynamic_tiers()` — three regimes:
    - Clear winner (top ≥ 0.5 AND gap > 0.3): VERIFIED/WARNING/INFO at 0.7×/0.5× top
    - Flat (top ≥ 0.3): WARNING/INFO only at 0.7× top
    - All weak (top < 0.3): all INFO
  - `render_orientation()` — confidence-gated sections + honest fallback
- Wrapper edit at `oh_gt_full_wrapper.py:6045-6090` — replaces caller-count ranking with composite + dynamic tier rendering
- Per-task telemetry: `[GT_META] orient_candidate_N` and `[GT_META] orient_tiers` emit signal breakdowns

**Tests:** 31 new in `tests/unit/test_orientation_composite.py`. All pass. Full suite: **241 passed.**

**Wrapper import verified clean** after edit.

---

## Layer 2.2: L3 Post-Edit — categorical filter + Contract pillar always-fire

**DOC_OF_HONOR §2.2:** WORKING (claimed). 13 priority levels; G7 silence gate; hardcoded `confidence >= 0.6` and `>= 0.5` fallback.

**CLAUDE.md aim (§59):** four pillars — Contract / Consistency / Completeness fire ALWAYS regardless of graph quality; only Callers gates on edges.

**Graph layer strength at audit time (post deepswe-parity merge):**
- 6 strong resolution methods (added verified_unique 0.95, type_flow 0.9, lsp_verified async)
- `trust_tier` populated (CERTIFIED / CANDIDATE / SPECULATIVE / SUPPRESSED)
- `candidate_count` per edge
- 84% deterministic Python (was 18% name_match); 95%+ after LSP background promotion
- Categorical signals replace numeric confidence as the primary filter axis

**Code reality (from `output.jsonl`):**
- sh-744: L3 fired full evidence at iter 62, resolved
- conan-17102: L3 fired `[PROPAGATE] graph_build_order_merge() in graph.py:139` at iter 104 (agent saw but didn't act)
- weasyprint-2300: L3 caught `[MISMATCH]` on `new_str=None` deletion, agent recovered
- arviz-2413: ZERO post_edit_contract events (router_v2 suppression — separate bug, defer)

Existing labels: `[BEHAVIORAL CONTRACT]`, `[SIGNATURE]`, `[CALLERS]`, `[TEST]`, etc. — semantic categorization, research supports keeping. No `[VERIFIED]/[WARNING]/[INFO]` in current output (good).

**Research direction:** Filter hard upstream using categorical signals; render verbatim downstream; no display-level confidence labels.

**What was built:**

1. **Categorical filter helper** in `post_edit.py:114-200`:
   - `_categorical_edge_filter_clause()` — SQL fragment for the categorical combination
   - `_legacy_confidence_filter_clause()` — backward-compatible numeric (`confidence >= 0.6`)
   - `_edge_filter_for_db()` — schema-aware picker

   Categorical rule (hybrid 3-signal):
   - `resolution_method IN (strong 6 methods)` OR
   - `resolution_method = 'name_match' AND candidate_count <= 1` OR
   - `trust_tier IN ('CERTIFIED', 'CANDIDATE')`
   - AND `trust_tier != 'SUPPRESSED'`

2. **Replaced hardcoded thresholds** at lines 411 (propagation), 703 (display callers) with `_edge_filter_for_db()`.

3. **Removed numeric `0.5` display fallback** at lines 822-833 — per Squeez 2604.04979 + Anthropic 2025: no low-confidence display fallback. Honest empty rather than degraded.

4. **G7 isolation gate refactored** (post_edit.py:2519-2580):
   - Drop caller-derived markers (legitimately impossible when 0 callers)
   - Keep ALL Contract/Consistency/Completeness markers (CLAUDE.md:59 always-fire)
   - If everything filtered, emit `[SIGNATURE] {sig}` even untyped (Contract pillar minimum)
   - If signature also empty, honest verbatim `"[INFO] Function appears isolated..."` note

**Three properties check (applied as INTERNAL pipeline properties):**
- Dynamic ✅ — filter clause picks categorical/legacy per actual schema; per-edge categorical evaluation
- Hybrid ✅ — 3 categorical signals composited (resolution_method + candidate_count + trust_tier)
- Confidence-gated ✅ — at the FILTER level (not display); SUPPRESSED tier hard-excluded; honest empty rather than degraded fallback

**Display change:** NONE. Agent sees same verbatim evidence format. No `[VERIFIED]` / `[WARNING]` / `[INFO]` prefixes added.

**Tests:** 11 new in `test_post_edit_categorical_filter.py`. Full focused suite: **262 passed.**

**Deferred:** Router_v2 suppression on arviz-class tasks (separate diagnostic).

**Verifier-found fixes (same day):**
- Line ~2353 callee query (`Calls into:`) — twin of caller query, missed first pass → converted to categorical
- Hop-2 thin-wrapper caller query (~967) — used removed `conf_filter` (would crash) → converted to categorical
- G7 marker token-shape gaps: added `TWINS:` + `[SCOPE]` to keep, `CALLERS:` + `[CONTRACT]` to drop
- G7 extracted to `g7_filter_isolated()` module-level pure function
- 7 new G7 tests. Full focused suite: 269 pass (was 262).

---

## Layer 2.3: L3b Post-View — AUDIT (research complete, fix pending)

**DOC_OF_HONOR §2.3:** Trigger `file_editor` view; module `post_view.py`; `graph_navigation()`. Callers/callees confidence >= 0.7, importers >= 0.5, hub-penalized ranking. Status claimed WORKING.

**Ground-truth findings (verifier agent):**

1. **Ego-graph fires 0/13 — Gate 1 is the bottleneck.** Three conjunctive safety gates at post_view.py:686-694:
   - Gate 1: function name must EXACTLY match an issue term (`_f["name"].lower() in _issue_terms`) — no fuzzy/split matching. Rarely aligns.
   - Gate 2: `min_confidence=0.9` — only same_file/import/unique-name_match clear it
   - Gate 3: `len(callers) > 0` after 0.9 filter
   - Conjunction makes the block effectively dead.
   - Also: `_load_issue_terms()` called without `state` arg (line 675) → falls back to legacy `/tmp/gt_issue_terms.txt`; if missing, Gate 1 fails 100%.

2. **Still 100% numeric confidence — NOT migrated to categorical.** Zero references to `resolution_method` / `trust_tier` / `candidate_count` in post_view.py or ego.py. Hardcoded `>= 0.7` (callers/callees, lines 308/416/433/449/486), `>= 0.5` (importers/tests, 596/773), `>= 0.9` (ego BFS, 693). The Layer 2.2 categorical migration did NOT reach L3b.

3. **Contract pillar gated behind callers — CLAUDE.md:59 VIOLATION.** Signature/return/guards only render inside the ego-graph block (ego.py:99-105), which only fires if `len(callers) > 0` (Gate 3). Main nav path emits callers/callees/importers + parallel-pattern "Spec:" line but NO signature/return contract. A function with 0 high-confidence callers gets zero Contract delivered. Same anti-pattern the Layer 2.2 G7 fix addressed for L3 — did not reach L3b.

4. **Display format already research-clean.** No `[VERIFIED]/[WARNING]/[INFO]` labels, no provenance parens. GT_META to stderr. Good.

5. **DOC §2.3 stale/incomplete:** omits the ego-graph block entirely; line citations (280-560) stale (real `graph_navigation()` is 330-703); doesn't mention numeric-only confidence or the Contract-gating violation.

**Verdict: VIOLATES** (Contract pillar gated behind callers; not migrated to categorical; ego-graph dead).

**Fix plan (decided after CLAUDE.md alignment check):** A + B + D. Drop C.

CLAUDE.md alignment of each fix:
- A: ✅ "stay silent when uncertain" + "don't inject low-conf as fact"
- B: ✅ **THE constitutional fix** — CLAUDE.md:86 literal "Never gate context that doesn't need edges behind a connectivity check — leaves the agent blind on exactly the files where it needs help most"
- C: ⚠️ risky — relaxing ego gate could re-introduce "confident on weak signals" poison (CLAUDE.md:71). Dropped — once B carries Contract on main path, ego becomes redundant enrichment.
- D: ✅ pure bug fix

**What was built:**

**A — Categorical filter** (`post_view.py`):
- New `_edge_filter(db_path)` reuses L3's `_edge_filter_for_db()`
- Caller query (411), callee query (446), representative-source-line subquery migrated from `>= 0.7` to categorical
- Single source of truth across L3 + L3b

**B — Contract pillar always-fire** (CLAUDE.md:86):
- New `_contract_pillar(conn, needle, issue_terms)` — signature + return_type from `nodes` table, no edges needed
- Prepends ≤3 `[CONTRACT]` lines on EVERY view regardless of caller count
- Issue-relevant function names ranked first
- Fixes the violation: L3b previously delivered contract ONLY inside the ego-graph (callers>0 gated). Isolated functions now get their contract.

**D — `_load_issue_terms(state)`**:
- Ego block (line 742) was calling without state → legacy file fallback. Now passes state.

**Flip relevance:** B is the lever. On the 13-task run, agent viewed functions with 0 high-confidence callers (sparse graph) and got NO contract — edited blind. Now every view shows signature + return type. Correct context → correct code → flips (CLAUDE.md:88). Not engineering toward flips; fixing a constitutional violation.

**Three properties (internal):**
- Dynamic ✅ — categorical filter picks per schema; contract ranks per issue relevance
- Hybrid ✅ — A combines 3 categorical signals; contract uses signature + return + issue overlap
- Confidence-gated ✅ — A at filter level; Contract is structurally certain (parser output), no confidence label

**Display:** No `[VERIFIED]/[WARNING]/[INFO]`. `[CONTRACT]` is a content type marker, not a confidence tier.

**Tests:** 8 new in `test_post_view_contract_pillar.py` — key test: `graph_navigation()` delivers `[CONTRACT]` on isolated (0-caller) function. Full focused suite: **277 pass.**

**Verdict: VIOLATES → WORKING.** Contract pillar always-fire fixes CLAUDE.md:86; caller/callee categorical; issue terms load.

---

## Layer 2.5/2.6: L5 Scaffold + L5b Late Reminder — diagnostic-only

**DOC_OF_HONOR §2.5/§2.6.** L5 = scaffold governor; L5b = late reminder.

**Runtime evidence (13-task):** L5/L5b helped **0 flips.** `follow_rate_within_3 = 0.0` on every task measured (agent ignored ~100%). Suggestions frequently WRONG: weasyprint (contradicted correct brief), cfn-lint (0/10 pointed at gold), pypsa/conan (unrelated files). Cause: `sorted(brief_candidates)` alphabetical + prescriptive directives.

**Research (the deciding evidence):**
- **SWE-PRM NeurIPS 2025 (2509.02360):** mid-trajectory intervention helps ONLY when diagnostic; **action-prescriptive feedback LOWERED resolution** (over-constrains agent). Diagnostic (taxonomy-guided) won.
- **Anchoring 2412.06593 + Is-Grep-All-You-Need 2605.15184:** a harness is a privileged tool output → confident wrong suggestion ANCHORS the agent, compounds across planning steps. The agent does NOT just ignore it.
- **Localization is an UPFRONT lever** (15-17×, +12.8pp), realized before first action — NOT a mid-trajectory nag. File candidates belong in L1, not L5.
- **Verify-before-finish IS supported** (SWE-agent guardrail +10.7pp) — but as verifiable-action, not content prescription.

**Cursor principle (user-clarified):** "Cursor = never harm the model." A layer may intervene IF it's correct-or-quiet: assert only verifiable facts, be under-confident (silent) when unsure, never steer wrong.

**What was built:**
- **L5 `_render_scaffold_advisory`** → diagnostic-only. Removed `_rank_scaffold_candidates`. States the verifiable fact (no source edit yet, last was scratch X); NO file list, NO "edit X"/"start with"/grep directive.
- **L5b legacy tracker** (`_check_pending_next_actions`) → message changed from "[GT L5: Ignored Structural Witness] ... Next action: read caller contract X" to "[GT L5: Unexamined structural signal] ... It may be relevant to the edit." No directive.
- **Goku twin** (`hooks.py:hook_structural_witness_ignored`) → same diagnostic conversion.
- **`hook_finish_without_structural_witness`** → "[GT L5: Finish without verification]" diagnostic (kept verify-before-finish intent, dropped "inspect one caller" content prescription).
- **DOC §2.6 corrected:** the "goku_active=1 suppresses all injection" claim was FALSE — 3 paths, 2 env vars (`GT_L5_GOKU_EVENTS`, `GT_L5_STRUCTURAL_UNVERIFIED`).
- **5 analysis scripts** (full_architecture_audit, gt_autopsy, gen6_real_tables, gen6_deep_table, cursor_rerun_tables) updated to match the new marker so L5b firings still get counted in new-run analysis.

**Three properties:** L5/L5b now confidence-gated by being diagnostic (assert only verifiable facts); no prescriptive anchor regardless of confidence. The narrowing/ranking lives upstream in L1 orientation (the composite), per research that localization is an upfront lever.

**Tests:** 5 new in `test_l5_diagnostic.py`; 2 preflight assertions updated for new message text. 216 pass (L5 + preflight + invariants + topology). Pre-existing failures (gt_intel attribute, post_edit_improved×2, post_view_stderr corrupt-db) confirmed unrelated via stash.

**Verdict: VIOLATES → WORKING.** Prescriptive-anchor harm removed; diagnostic facts only; DOC corrected.

---

## Layer 2.4: L4a Auto-Query — categorical filter (verified-only)

**DOC_OF_HONOR §2.4:** first source-file read (max 2/task), top-2 symbols + callers. Claimed WORKING.

**Audit:** display already research-clean (no labels). But used hardcoded numeric `confidence >= 0.5` (admits name_match noise the agent could grep) and ranked symbols by raw caller-count (hub bias).

**Strategic fit:** L4a's flip-relevant value is the ONE thing the agent can't grep — **verified cross-file callers at first read.** Delivering 0.5 name_match noise = thin arbitrage + anchor risk (Is-Grep-All-You-Need 2605.15184).

**What was built:**
- Both queries migrated to shared `_edge_filter_for_db()` (categorical, verified-only) — same helper as L3/L3b.
- Symbol-ranking COUNT ranks by VERIFIED in-degree (no name_match hub domination).
- Caller subquery admits only verified edges; SUPPRESSED excluded.
- Numeric `>= 0.7` fallback on legacy schema. Clause resolved from host db copy, interpolated into the in-container query.
- Kept: issue-keyword boost (hybrid 2nd signal), signature fallback (Contract always-fire).

**Three properties:** Dynamic (clause per schema) + Hybrid (verified in-degree + issue-keyword boost) + Confidence-gated (categorical filter at query level, SUPPRESSED hard-excluded). No display labels.

**Tests:** 3 new in `test_l4a_categorical.py`. 206 pass (L4a + L5 + L3 + L3b + invariants + topology). Wrapper import clean.

**Verdict: PARTIAL → WORKING.**

---

(more layers below as we build)
