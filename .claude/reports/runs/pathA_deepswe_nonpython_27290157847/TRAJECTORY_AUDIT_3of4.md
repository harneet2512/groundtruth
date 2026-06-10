# TRAJECTORY AUDIT — DeepSWE non-Python, 3 of 4 (run 27290157847) — 2026-06-10

Scope: go `abs-module-cache-flags`, js `csstree-shorthand-expansion-compression`, ts `arktype-json-schema-refs-dependencies`. (rust `boa-hierarchical-evaluation-cancellation` still running — follow-up.) GT-on, deepseek-v4-flash, substrate digest `d30f34b4…`, gt commit `96a2bf0c`. Trajectories read **fully and chronologically** from `mini-swe-agent.txt`; certs from `gt_artifacts/`; outcomes from pier verifier. Ledgers: `task_ledgers/<task>.md`; scorecards: `<task>/scorecard.json`.

---

## THE HEADLINE — does the trajectory make sense? Did graph depth translate into the agent using a TRUE map?

**One-line answer: YES on js and ts — the LSP-converted deep graph produced briefs whose #1 candidate WAS the gold file, and in both cases the agent's first file-open was that #1 candidate (arktype even said so: *"Let me start by reading the key files mentioned in the task description"*). NO on go — gopls warmed but converted 0 edges (`project_ready=False`), and independently of that, anchor seeding missed `require`, the brief's #1 was wrong (`install/install.go`), and the agent self-localized by grep. 0/3 resolved; all three failures are post-localization implementation correctness, not navigation. Leakage 0 everywhere.**

| task | resolved (reward) | trajectory verdict | gt_caused | consumed | right_trajectory | localization root cause (Tier3b) |
|---|---|---|---|---|---|---|
| go `abs-module-cache-flags` | NO (0.00000000) | agent **self-localized** (`grep require` step 10); brief #1 wrong, gold file absent from candidates; ~1 wasted step on install.go | **FALSE** | partial (one L5 nudge consumed productively) | NO (agent's own) | **RERANK/anchor-seeding** (anchors = `["BeginRepl","clear"]` only) on a GREEN substrate; gopls warm-but-0-converted logged separately |
| js `csstree` | NO (0.00000000) | **brief #1 = gold (`lib/lexer/Lexer.js`)**, opened FIRST (step 5, pre-grep); whole 835-line patch landed there, built on the brief-named match API | partial (fair-probe weak: issue says "the lexer") | **YES** | **YES** | **CORRECT** — LSP converted (37 promoted / 94 deleted), rerank right |
| ts `arktype` | NO (0.00000000) | **brief #1+#2 = edited gold (`object.ts`, `shared/jsonSchema.ts`)**, opened FIRST with an explicit consumption statement | partial (fair-probe moderate: issue names `parseJsonSchema`) | **YES (explicit)** | **YES** | **CORRECT** — deepest conversion (415 promoted / 903 garbage deleted, w_sem 0.50) |

Adversarial honesty on the wins: in both js/ts the issue text partially pre-localizes (csstree: "Add two methods to the lexer"; arktype: names `parseJsonSchema`). A competent agent likely finds these packages by grep. What GT demonstrably added: the **exact file** (not just the package), opened **before any search**, plus the match-API / Traversal-contract map the fixes were built on. That is consumption of a true map — but it is NOT a flip, and per gt_trial Tier-2 logic `gt_caused=0` on all three (fair-probe fails on js/ts; delivered-correct fails on go).

---

## Tier 3b — did GT behave as gt_gt intends? (the main thing)

**Pipeline conformance: YES on all 3 — every stage fired in order (graph → LSP → localization/rerank → brief), and the per-language LSP dispatch worked.** Per stage:

- **Graph base (gt_gt §2):** GREEN ×3. go det `85.27960526%` (1216 calls), js `83.31977217%` (1229), ts `74.34339475%` (5102). FTS5 probe ok ×3, closures populated.
- **LSP (gt_gt §3):** ONE surface, dispatched per language as designed.
  - **js: warmed AND converted** — typescript-language-server, 342 attempted → `resolved_promoted=37.00000000`, Verified 24 / Corrected 13 / **Deleted 94** garbage name_match; graph hash changed pre→post; closure rebuilt 1468→1547.
  - **ts: warmed AND converted at scale** — 1682 attempted → `resolved_promoted=415.00000000`, Verified 214 / Corrected 201 / **Deleted 903**; closure 4766→6460. This is the depth story working: 903 false edges removed BEFORE ranking.
  - **go: warmed but converted ZERO** — gopls `warm_probe_ok=true` (1.64175034ms) but `project_ready=False` after 20005ms; 7/7 attempted edges `lsp_error`; graph hash unchanged; `lsp` absent from the resolution distribution. **Open defect: gopls project-readiness in the task container** (likely GOPATH/module cache not primed). It did NOT cause the go mislocalization (det graph already 85%), but it means Go gets no method-call cleaning.
- **Embedder (gt_gt §5):** GREEN ×3, identical identity (gte-modernbert, 768d, cos 0.71040983/0.29940427). Note the query-adaptive Dimension-0 fired on ts: `effective_w_sem=0.50000000` vs 0.25 elsewhere — the 2026-06-10 fusion behaving dynamically, and on the task where dense-lead was right.
- **Rerank (gt_gt §4.2):** right on js/ts (gold #1). Wrong on go with all substrate gates GREEN → bucketed **RERANK/anchor-seeding**: `gt_issue_anchors.json` extracted only `{"symbols": ["BeginRepl", "clear"]}` from an issue whose real symbols are `require`, `ABS_MODULE_PATH`, `require_cache_info` — the backticked `require()` (with parens) and SCREAMING_SNAKE env vars never became anchors, so lexical lead latched onto "module"/"install" → `install/install.go` #1. **This is the fixable lever from this run: anchor extraction for `name()`-style calls and ENV_VAR tokens.**
- **Gates/certs (gt_gt §12 row "gates/certs"):** `GRAPH_FAIL_MISSING_HANDOFF` appears in ALL 3 graph certs and is the **known FALSE FAIL** (cert is pre-agent). The runtime witness contradicts it everywhere: brief delivered at turn 0 in `pr_description`, `hook_hash_match=true`, `gt_prebuilt_active=true`, live `<gt-evidence>` in agent observations. **However `outcome.json` mechanically stamps `cert_fail=true → failure_class="GT"` from it on all 3 tasks** — the false-fail is leaking into the tally layer and should be reconciled in the outcome writer (it makes every task look like a GT infrastructure failure when the handoff demonstrably worked).

## Did the 2026-06-10 localization + delivery + L5 fixes behave on non-Python?

- **Delivery (brief-consume fix):** YES — the in-container brief reached the agent at turn 0 inside `<pr_description>` on all 3; `[gt-patch:loaded]` confirmed at step 1; live hooks (`gt-scope`, `gt-evidence`, `gt-contract`, `gt-nudge`) appended in observations throughout. No GT_META leak, no empty dedup tags observed.
- **Localization (MaxSim granularity + dense floor + Dim-0):** translated on js/ts (gold #1; ts ran with w_sem=0.50 dense-lead). go exposes the residual gap: anchor seeding, not fusion.
- **L5 classifier fix (failure_persisted / loop):** **behaved correctly on all 3 — the headline win for the fix.**
  - go step 70: `failure_persisted` fired on a REAL `go test -run TestRequire` failure that the agent's own edit introduced — a true positive — and was **consumed**: the agent re-read the assertion, `git stash`-bisected, found the `index.abs` append bug, red→green by step 100.
  - js/ts: **zero false firings** despite dozens of failing scratch `node -e` probes (MODULE_NOT_FOUND, eval SyntaxErrors, TraversalErrors) — exactly the env/scratch suppression the fix added. No confident-wrong steer anywhere.
  - Residual noise: `scaffold_trap` fired 3/3 at ~25 actions during legitimate feature-scoping exploration (these are build-a-feature tasks, not bug-fixes; 25 read-only actions is normal). Harmless (agents ignored it), but it's a calibration item: the threshold should probably scale with issue type/size.
- **Vendored/builtin pollution:** **0** — no `node_modules/` path ever appeared in any GT payload on js/ts (agent's own `find` hit node_modules; GT's scope/evidence stayed in `lib/`/`ark/`).
- **Leakage:** **0** across all 3 (no hidden test names, no FAIL_TO_PASS; visible repo test names entered only via the agent's own greps).

## Why 0/3 resolved (vs board deepseek 0.790, unpaired)

All three submitted right-place patches that fail hidden tests on **implementation semantics**:
- go: module-path candidate resolution details + debug-trace `cache` event on the OS-env fallback (`TestChallengeRequire*`).
- js: compression must emit canonical-order values OMITTING initials per layer (`'bold 16px/1.5 Arial'` expected vs `'normal normal bold normal 16px/1.5 Arial'` produced); agent's compress concatenates everything.
- ts: the issue's explicit note "Ensure enum deep equality with object/array values" was never implemented (the agent fixed the `type.enumerated` spread but not deep equality), plus a `$ref`-in-dependentSchemas runtime throw.

This matches the 30-task Python finding (gt_gt §13): dominant failure = **post-localization implementation correctness**, the layer GT does not yet address. These three are spec-heavy feature-builds where the residual gap is "did the agent satisfy every clause," not "did it find the code."

Two run-infra flags (not GT): (1) `artifacts/model.patch` capture includes side-effect files the agent did NOT submit (csstree: `package-lock.json` hunk; arktype: ~300KB regenerated `ark/docs/components/dts/schema.ts` from `pnpm build`) — if the verifier applies model.patch rather than the submitted `/tmp/patch.txt`, build-artifact pollution could affect grading; (2) the `GRAPH_FAIL_MISSING_HANDOFF` → `failure_class=GT` mislabel in `outcome.json` (above).

## Action items surfaced
1. **Anchor extraction (localization lever, generalized):** capture backticked `name()` call tokens and `UPPER_SNAKE` env-var tokens as anchors — go's `require`/`ABS_MODULE_PATH` miss is the entire go mislocalization.
2. **gopls project-readiness:** prime the Go module env in the task image (or extend readiness wait/`GOFLAGS=-mod=mod`) — currently Go gets zero LSP conversion (`project_ready=False`, 7/7 lsp_error).
3. **outcome.json reconciliation:** stop classifying `failure_class=GT` from the known-false `GRAPH_FAIL_MISSING_HANDOFF` when `hook_hash_match=true ∧ gt_prebuilt_active=true`.
4. **scaffold_trap calibration:** suppress or delay on feature-build-shaped issues (no traceback, multi-clause spec).
5. **model.patch capture hygiene:** exclude lockfiles/build outputs the agent did not list in `/tmp/patch.txt`.
