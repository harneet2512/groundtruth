# Session Summary

## Date / Time
2026-06-09 (full-day arc; supersedes the morning entry)

## Branch / Commit
`gt-trial` @ `4253da65` (go/no-go: 5/5 language-agnostic smoke EXECUTED on the fixed-stack
substrate, run 27249519490). Pushed to **BOTH origin/harneet2512 AND hbali-stack** (user
override of the prior "code→origin only" rule, to enable the DeepSWE runs on hbali).

## Objective
The day's arc: **levers → audits → 62 findings → 4 fixes → rebuild.**
1. Land the localization levers (CHANGE 1 granularity, fusion+floor, CHANGE 2 embedder swap).
2. Run 4 parallel LIPI reviewers over the whole DeepSWE-proof stack (pipeline+gates /
   localization / delivery / indexer) → **62 findings**.
3. Ship the four fixer surfaces (each red→green proven), rebuild the substrate on the fixed
   stack, re-fire the proof waves.
4. Bring `gt_gt.md` (the architecture source of truth) back to CURRENT (this session's doc pass).

## Files read (key)
`gt-index/internal/resolver/{resolver,relationships,api_edges}.go`,
`gt-index/internal/{store/incremental,closure/closure,parser/parser}.go`,
`src/groundtruth/pretask/{v1r_brief,v7_4_brief,graph_localizer,anchor_select}.py`,
`src/groundtruth/memory/enrich/embed.py`, `scripts/swebench/gt_run_proof.py`,
`scripts/metrics/foundational_gates.py`, `scripts/verify/deepswe_outcome.py`,
`artifact_deepswe/{gt_agent,gt_mini_patch}.py`, `.github/workflows/deepswe_full.yml`.

## Exact decisions used
- Levers merged: CHANGE 1 per-symbol MaxSim (`33970b9f`); **fusion + dense floor `5a6e99b4`**
  (the morning entry mis-cited `8a65b4ce`, which is the gt_gt §13 docs commit); Dim-1
  max-compose + sparse-floor (`dc5844f8`); **CHANGE 2 gte-modernbert swap `5f460f23`**.
- Dense-weight LOCKED: dense-led, `W_SEM=0.40` default + `W_SEM_FLOOR=0.25` enforced LAST,
  never throttled below the floor, lexical-fused (not monopoly).
- DeepSWE proof path is STRICT: `GT_GATES_DELIVER_ALWAYS=0` (any OFF gate fails); OH live
  path stays deliver-always (=1). OH workflows pin `GT_EMBED_MODEL_NAME=e5`; gte on substrate.
- Substrate graph is AUTHORITATIVE in proof mode → L6 single-file reindex deliberately OFF
  there; the restore-level LSP-strip itself fixed in the indexer (`10368a2f`).

## Research checked
ColBERT MaxSim (SIGIR 2020), MaxP (SIGIR 2019); CoIR/CodeXEmbed/CodeSage (code-IR); BEIR
(NeurIPS 2021), DPR (EMNLP 2020), Sciavolino (EMNLP 2021); XTA (Tip&Palsberg OOPSLA 2000),
PyCG (ICSE 2021), JARVIS 2023, ACG (ECOOP 2022) — the resolver-rung + builtin-drop basis.

## Implementation changes (committed + pushed)
- **Levers:** `33970b9f` CHANGE 1 · `5a6e99b4` fusion+floor · `5f460f23` CHANGE 2 (gte) ·
  `72e688b4` no-fallback hardening · `f807e... / f6294add` substrate bakes (LSP servers, Go
  toolchain) · `d6066ba3` run_manifest v2 provenance.
- **The 4 LIPI fixer surfaces (62 findings):** `9bf106ca` pipeline+gates (P0 green-zero-run
  chain fail-closed end-to-end; FAIL_NO_WARM/INSTALL_MISSING exit 2; per-lang certs +
  aggregation; cert schema v2 + version-skew FAIL; brief.txt = 8th required artifact) ·
  `dc5844f8` localization (model-keyed cache, sparse W_SEM floor, witness provenance honesty,
  Dim-1 compose, ST hole) · `ffc6c7dc` delivery (exact-match pillars, verified-caller counts,
  sanitizers, ro/immutable connect + one-time probe, adapter raise visibility) · `10368a2f`
  indexer (Go CHA arity, 1.9 rung reorder, deterministic `-file` restore, AND-rule closure,
  phantom/field_read guards, relationship/API trust stamping). Plus `8ae5584d` gopls launch.
- **Rebuild + waves:** `02b02425` substrate rebuild on the fixed stack · `0e2489cc` wave-2
  re-fire · `4253da65` smoke 5/5 executed.
- **Docs (this pass, uncommitted by instruction):** `gt_gt.md` reconciled to HEAD with dated
  UPDATED notes (§2.1/§2.3/§2.5/§4.1/§4.2/§4.3/§5/§6/§7/§11.8/§12/§13.1/§13.4 + new §13.7);
  `LATEST_TASK.md`; this file.

## Metrics before → after
- CHANGE 1 (Stage-1, real graph e5/384): gold #1 **3/7 → 7/7**; synthetic separation 0.02→0.67.
- Fusion+floor: 15/15; `effective_w_sem ≥ 0.25 > 0` on every classification.
- Fixers: 9bf106ca 22 red → 226 fail_closed pass · dc5844f8 15/17 red→green, 292 regression
  pass · ffc6c7dc 26 fail → 52 pass · 10368a2f red→green vs pristine-HEAD worktree, closure
  −19.7% rows on dagster with zero deterministic edges lost.
- Smoke (fixed stack): **5/5 languages**, identical gt-run-proof command, exit 0, warm LSP
  (all NO_OP_VALID_WITH_WARM_SERVER — fixtures resolve structurally; ACTIVE LSP is the
  113-sweep's question).

## Tests / runs executed
Per-fixer red→green suites (above); go build exit 0 + `go test` (1 known pre-existing
failure); 5-language smoke run 27249519490; 113-task non-paid substrate sweep dispatched
(running at session close).

## Result
Stack hardened on all four surfaces and PROVEN live on the rebuilt substrate (smoke 5/5).
gt_gt.md is CURRENT again (dated supersede notes, no silent rewrites).

## Regressions
None new. Known pre-existing: 1 resolver go-test failure (TestRoutePatternMatching, fails on
base too); render/fail_closed pre-existing failures unchanged.

## Rollback
Each fixer surface is a single revertable commit (`9bf106ca`/`dc5844f8`/`ffc6c7dc`/`10368a2f`);
substrate rollback = re-pin the previous image digest.

## Open blockers
113 sweep completion → integration audit; real-repo ACTIVE LSP resolution unproven (smoke was
no-op-valid on tiny fixtures); 1-task dry gated on sweep + integration audit; benchmark on D2.

## Next allowed action
Read the 113-sweep verdicts → integration audit (gt_trial §4 style, agent-observation) →
1-task dry → paired GT-on vs GT-off decision (Wilcoxon) across all 5 languages.

---
*Architecture of record: `gt_gt.md` §11–§13 (incl. the new §13.7 hardening). This session's
docs live there + here.*
