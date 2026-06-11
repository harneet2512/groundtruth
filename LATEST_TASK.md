# LATEST_TASK.md — Session Handoff (2026-06-11)

## STATUS: 6 BUGS TO FIX → REBUILD → BENCHMARK

The delivery engine is built, tested (547 green), and pushed to hbali (`fe128e0f`).
Three measurement runs completed (baseline `27307362054`, oracle-only `27321848581`,
delivery `27342218002`). M20 +10 hidden tests (code quality improving). 0 flips.
The deep gt_trial audit found 6 specific bugs that explain why. Fix them → benchmark.

## THE 6 BUGS (exact sites, from the deep audit `task_ledgers/RUN_27342218002_SUMMARY.md`)

### BUG 1 — LSP 0/0 ACROSS ALL 5 LANGUAGES (P0, root cause UNKNOWN)
- **What:** LSP converts ZERO edges on every language (go/py/ts/js/rust) on digest `55f18a1c`.
  The PRIOR digest (`1d4d8bfd`) converted 415 on ts, 167 on py. Something broke between digests.
- **Impact:** graph depth regressed to tree-sitter-only. ALL delivery engine results are on a DEGRADED graph.
- **Certs:** every task shows `converted=0`, `failed=N`, `verdict=LSP_ACTIVE_VALID` (the gate PASSES a 0-conversion LSP — §7 violation).
- **Action:** investigate what changed between digests `1d4d8bfd` → `1e932985` → `55f18a1c` in the LSP resolve path. Check `src/groundtruth/resolve.py` for regressions. The substrate bakes the resolve code — a src change that broke LSP would be invisible until a rebuild.

### BUG 2 — SELF-VERIFICATION GATE HARMS 4/9 (disable or fix)
- **What:** `gt_agent.py:871-877` `_ENV_UNVERIFIABLE_RE` is incomplete. Missed `syscall/js` (go), `TS2307` (ts), corepack/yarn (js). The L5 governor's env classifiers exist but were never ported to the gate.
- **Impact:** 4/9 actively harmful (agent wrote `os.Setenv` hacks, resubmitted byte-identical, burned 60 steps on yarn).
- **Action:** port `_ENV_FAIL_RE` patterns from `gt_mini_patch.py:1718-1730` to `gt_agent.py:871-877`. Or: disable the gate (`GT_SELF_VERIFY_ATTEMPTS=0`) until the classifier is correct.

### BUG 3 — OBLIGATION SILENT AT SUBMIT 8/9
- **What:** `gt_oracle.py:937-973` the `oblig_class_spent` one-shot budget gets spent mid-run on an early review-transition, then the obligation goes SILENT at the actual pre-submit moment.
- **Impact:** the #1 lever (review-transition obligation) fires early and is exhausted when it matters.
- **Action:** guarantee a pre-submit fire: reserve one emission for the FINAL review-transition (within last 2×V steps), regardless of prior spend.

### BUG 4 — OBLIGATION RENDER TRUNCATED + WRONG
- **What:** the run-deciding clause is cut to "(+N more unverified)". False "✓ edited" rows from cross-attempt blindness, scratch file over-crediting, token-overlap≠implemented. Covering-test text is garbage (`the test suite`, `npx jest` in a Rust repo).
- **Action:** un-truncate the obligation table. Fix edited? to require the symbol in a SOURCE file edit (not scratch). Fix covering-test render to use the actual `_test_run_command` output.

### BUG 5 — CONFIDENCE TIER VIOLATION
- **What:** every C1 emission at 0.02-0.4 confidence. The `[INFO] never-on-C1` rule from §15.3 is absent — only the relative median+MAD floor exists.
- **Action:** add an absolute floor: confidence < 0.5 → suppress from C1 (the agent-visible channel). Render to telemetry only.

### BUG 6 — COSMETIC/PLUMBING (4 items)
- Doubled `<gt-task-brief>` (root: `gt_agent.py:610` `_prepend_brief` startswith guard)
- `[gt-patch:loaded]` leak (internal marker visible to agent)
- `GT_REQUIRE_EMBEDDER=1` fail-open (numpy missing → embedder silently OFF → §7)
- deep-metrics blind (`per_layer={}`)

## WHAT WORKS (confirmed live on delivery run 27342218002)
- Governor FP closure: 0 false `failure_persisted` (was 3/3 two runs ago)
- Full-declaration signatures: boa renders `T: Trace + 'static` correctly
- Coherence detector: consumed true-positives on 5-6 tasks (best new layer)
- abs-stepped localization FIXED (wrong `New` pin didn't recur)
- aiomonitor tailwind.js brief ranking FIXED (didn't recur)
- scaffold_trap RETIRED on oracle route (no more research-invalidated fires)
- Cross-lang fact-row filter: held across all tasks
- Oracle telemetry harvest: wired (verify on next run)

## THREE MEASUREMENT RUNS ON DISK (frozen, for comparison)
1. **Baseline** `27307362054` (pre-oracle GT, deepseek): `.claude/reports/runs/tenpack_27307362054/`
2. **Oracle-only** `27321848581` (oracle, no self-verify): `.claude/reports/runs/oracle_tenpack_27321848581/`
3. **Delivery engine** `27342218002` (full engine, deepseek): `.claude/reports/runs/delivery_tenpack_27342218002/`

Metrics reports: `.claude/reports/metrics/oracle_vs_baseline_20260611/` and `delivery_vs_baseline_20260611/`

## THE 10 TASKS (2 per language)
go: abs-module-cache-flags, abs-stepped-slices
js: csstree-shorthand-expansion-compression, katex-multicolumn-array-spans
ts: arktype-json-schema-refs-dependencies (infra-fails consistently), awilix-async-container-initialization
rust: boa-hierarchical-evaluation-cancellation, fd-deterministic-multi-key-sorting
py: adaptix-name-mapping-aliases, aiomonitor-task-snapshots-diff

## KEY METRICS (delivery engine vs baseline)
- M20 hidden test pass: 187→197 (+10, directional, p=0.31)
- M13 steps-to-gold-edit: 33→51 (+15, slower — agent explores more)
- M03 total steps: 145→134 (-11, agent finishes sooner overall)
- M04 P@3 brief: 0.63→0.67 (improved)
- Resolved: 0→0 (no flips)

## SEQUENCE TO BENCHMARK
1. Fix BUG 1 (LSP regression — investigate + fix)
2. Fix BUG 2 (disable self-verify gate OR port env classifiers)
3. Fix BUG 3 (guarantee pre-submit obligation)
4. Fix BUG 4 (un-truncate + fix edited? + fix covering-test)
5. Fix BUG 5 (confidence floor on C1)
6. Fix BUG 6 (cosmetic/plumbing)
7. Push + substrate rebuild
8. 10-task validation run
9. gt_trial FULL audit (start-to-finish, per-layer TABLES, not summaries)
10. If clean → full 113-task benchmark

## KEY FILES
- Delivery engine: `artifact_deepswe/gt_mini_patch.py`, `gt_oracle.py`, `gt_oracle_sense.py`, `gt_agent.py`
- Substrate: `src/groundtruth/pretask/v1r_brief.py`, `curation_map.py`, `contract_map.py`, `spec.py`
- LSP: `src/groundtruth/resolve.py`
- Indexer: `gt-index/internal/parser/parser.go`
- Pipeline: `.github/workflows/deepswe_full.yml`
- Architecture: `gt_gt.md` (§1-§16 + REFERENCES)
- Audit artifacts: `task_ledgers/RUN_27342218002_SUMMARY.md`, `DEEP_TRAJECTORY_ANALYSIS_ORACLE_RUN.md`
- Gap analysis: `GAP_ANALYSIS.md`, `RESOLVED_ISSUES_CHECKLIST.md`

## REMOTES + SECRETS
- hbali-stack is THE working repo (not origin/harneet2512)
- Pre-push guard: `.git/hooks/pre-push` (blocks reports/secrets)
- Secrets: `DEEPSEEK_API_KEY`, `TOKENROUTER_API_KEY` on hbali-stack
- Current digest: `55f18a1c` (HAS THE LSP BUG — investigate before using)
- MiniMax-M3: wired but fails (TokenRouter doesn't support Responses API; needs model_class override)

## CLAUDE.md RULES STILL IN FORCE
- No LLM in the GT pipeline
- Dynamic / hybrid / confidence-gated (3 mandatory properties)
- Generalized (any repo/lang/agent/model) — no benchmaxxing
- Correct-or-quiet (silence > wrong)
- DONE = metrics changed (flips or efficiency delta, Wilcoxon p<0.05)
- gt_trial audit = read trajectories chronologically, compare against gt_gt, produce TABLES not summaries
