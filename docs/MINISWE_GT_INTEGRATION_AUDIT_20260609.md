# MINI-SWE × GT INTEGRATION — POST-FIX CONFORMANCE AUDIT (read-only)

**Date:** 2026-06-09 · **Branch:** `gt-trial` · **HEAD:** `0e2489cc` ·
**Audited against:** `gt_gt.md` §6 / §12 / §13 (working-tree copy; the only uncommitted
gt_gt delta is the §5 embedder-supersession note — flagged where relevant) + the post-fix
code reality of the 4 fix surfaces (`9bf106ca` pipeline+gates, `dc5844f8` localization,
`ffc6c7dc` delivery, `10368a2f` indexer).
**Scope rule honored:** every claim below is from a direct Read of HEAD code (all audited
chain files are CLEAN in `git status`; only `gt_gt.md` is mid-update). Anything only a live
trajectory can prove is marked **UNVERIFIED-NEEDS-RUNTIME** and appears in the D2 checklist.

**Verdict summary:** the chain **CONFORMS link-by-link at the code level** — fail-closed is
real at every seam audited (empty issue, empty brief, missing/divergent graph, never-warm LSP,
swallowed adapter raise, zero-step green runs), the per-turn pillars carry the exact-match /
verified-count / sanitizer / ro-connect fixes, L6 is deliberately gated OFF in substrate mode
(gt_gt §6's "per-edit reindex" row is the doc-mid-update divergence the prompt pre-flagged),
and the exit path classifies witness-missing→GT with line-anchored INFRA. **6 NEW gaps**
(none P0; one classification-fidelity, one cosmetic double-tag, four hardening notes) and
**12 UNVERIFIED-NEEDS-RUNTIME items** = the D2 success criteria.

---

## (a) Link-by-link conformance table

Status legend: **CONFORMS** (proven by code Read, often + a fail-closed test) ·
**GAP** (see section b) · **UNVERIFIED-NEEDS-RUNTIME** (U-numbered; see section c).

### Link 1 — Substrate → artifacts (`gt-run-proof`)

| # | Behavior @ file:line | gt_gt clause | Status |
|---|---|---|---|
| 1.1 | 8-artifact contract **including brief.txt**: `REQUIRED_ARTIFACTS` `scripts/swebench/gt_run_proof.py:33-42`; workflow re-verifies all 8 with `test -s` (non-empty) `deepswe_full.yml:500-505` | §13.5 (substrate emits the proof certs), §7 (no silent fallback) | CONFORMS |
| 1.2 | **Fail-closed empty/failed brief**: `emit_brief` returns `(False, …)` on raise OR empty text — "no swallow in proof", "no host fallback" `gt_run_proof.py:440-469`; `main` exits 2 with `GT_ARTIFACT_MISSING: brief.txt` `:703-707`. Red→green tests: `tests/fail_closed/test_portable_substrate.py::test_emit_brief_empty_is_fail_closed/_exception_is_fail_closed` | §4 (brief = `generate_v1r_brief`), §7 | CONFORMS |
| 1.3 | **Fail-closed empty issue**: `deepswe_full.yml:443-474` reads `instruction.md` FIRST (the P0.1-a fix — task.toml has no issue field), task.toml fields as fallback, exits 1 with `GT_ISSUE_MISSING` (teed into `trial_output.log`) when empty. Note: `gt_run_proof.py:533-543` alone tolerates a missing issue (writes empty `issue.txt`, "free liveness proof", `max_edges=500`) — by documented design; the workflow owns the empty-issue gate on the paid path | §7; the 9bf106ca "green-zero-run chain" kill | CONFORMS (division of labor noted) |
| 1.4 | Boundary + baked deps + leakage: `validate_proof_env` (8 flags, baked LSP servers derived from `lsp/config.py::LSP_SERVERS`, baked **configured** embedder = gte only, no e5 substitution) `:218-300`; `assert_container_boundary` `:505-512`; `eval_leakage` env+top-level-file anti-cheat `:324-348,516-520` | §7 gates; §5 mid-update note (gte = configured model) | CONFORMS |
| 1.5 | **Per-language LSP, demand-scoped, aggregated fail-closed**: `_detect_langs` (ALL graph languages with servers) `:357-367`; demand scope via FTS5 issue terms (`max_edges` 20000 scoped / 500 issueless) `:392-409,575-588`; per-language certs `lsp_certificate_<lang>.json`, **no overwrite**, dominant copied canonical `:594-621`; `aggregate_lsp_verdicts` fails closed under `GT_REQUIRE_LSP=1` on `LSP_INSTALL_MISSING` / **`LSP_FAIL_NO_WARM`** / `LSP_RESOLVE_ERROR` / no-language-resolved `:412-437,627-636`. Tests: `test_portable_substrate.py::test_aggregate_*` (5 cases) | §3 (LSP one surface), §7, CLAUDE.md demand-driven | CONFORMS (live warm-up per language = **U7**) |
| 1.6 | Embedder cert guaranteed + **classified fail-closed** (degenerate/zero/ST-under-ONNX verdicts exit 2) `:653-693` | §5 (+mid-update), §7 `GT_REQUIRE_EMBEDDER` | CONFORMS (real bake = **U12**) |
| 1.7 | `run_manifest.json` v2 provenance — recorded-or-null gt_git_commit / substrate_digest / task_repo_commit / runtime_flags / language_distribution / graph_db_sha256 / cert_versions `:185-210` | §13.1 Stage-5 provenance | CONFORMS |

### Link 2 — Workflow → harness (`deepswe_full.yml` → pier)

| # | Behavior @ file:line | gt_gt clause | Status |
|---|---|---|---|
| 2.1 | Pinned-substrate fail-closed: digest required (`GT_REQUIRE_PINNED_SUBSTRATE=1` default), `@sha256:` immutability asserted, pull fail-closed `deepswe_full.yml:376-393,412-415` | §13.5 (pinned substrate image) | CONFORMS |
| 2.2 | pier launch: `--agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent`, `--env docker`, `--ak config_file=…deepswe_gt_pier.yaml` (`step_limit: 300`, `cost_limit: 5.0`; the instance_template's GT instructions match the patch's ACTUAL tags — `[WITNESS]`/`[CALLEE]` facts vs labeled "(unverified)" hints) `:572-588` + `deepswe_gt_pier.yaml:40-61,136-137` | §13.2 (pier + mini-swe-agent) | CONFORMS |
| 2.3 | **`--ae` env coverage**: forwarded = GT_HOST_GRAPH_DB(/gt_artifacts/graph.db), GT_CERT_DIR(/gt_artifacts), GT_HOST_SRC_ROOT, GT_PORTABLE_SUBSTRATE, GT_FORBID_PREBUILT_GRAPH, GT_PROOF_MODE, GT_CONTAINERIZED, GT_RUNTIME_STRATEGY `:579-586`. Cross-checked against every env READ in `gt_mini_patch.py` (GT_HOST_GRAPH_DB:239, GT_CERT_DIR/GT_PORTABLE_SUBSTRATE/GT_PROOF_MODE:221-224,242-246,417, GT_GRAPH_DB fallback:245, GT_ROOT_FILE/GT_HOOK_TIMEOUT/GT_INDEX_CACHE/GT_INDEX_BIN = safe defaults, GT_BASELINE = deliberately absent on the GT-on arm): **every consumed var is injected or safely defaulted**. Host-side `gt_agent` reads come from `$GITHUB_ENV` (`:511-520`) + top-level env — also complete | §6 (hooks need the substrate graph), §13.4 | CONFORMS (pier's internal forwarding chain is external code = **U4**) |
| 2.4 | `--mounts-json` **read-only bind** `/tmp/gt → /gt_artifacts` `:556-563` — container paths (not host paths) handed to the in-container patch; ro ⇒ the patch can never mutate the substrate graph | §13.5; the ffc6c7dc ro/immutable connect | CONFORMS (live readability = **U5**) |
| 2.5 | **pipefail + `${PIPESTATUS[0]}`** so `pier … \| tee` can't mask rc `:570-589,602-605`; **pier-swallow grep**: `grep -RIq "DeepSweAdapterError" jobs trial_output.log` fails the step on a swallowed adapter raise `:597-601`; job pinned to bash `:200-202` | the 9bf106ca P0.1-b fix | CONFORMS |
| 2.6 | Host installs ONLY pier + `pip install -e .` for adapter imports (no host LSP/gt-index — §B-AFTER no-host-GT) `:241-253,317-330`; PYTHONPATH includes `scripts/metrics` for the canonical witness formatter | §13.5 / §7 boundary | CONFORMS |
| 2.7 | Stage-0 prepare is env-only with a **no-graph.db legitimacy guard** `:181-191`; workflow_lint runs pre-matrix `:117-125` (`scripts/verify/workflow_lint.py` exists; `tests/fail_closed/test_workflow_lint.py`) | §7 legitimacy | CONFORMS |

### Link 3 — Injection point 1: instruction (`gt_agent.py`)

| # | Behavior @ file:line | gt_gt clause | Status |
|---|---|---|---|
| 3.1 | **Substrate-consume brief, no host fallback in proof**: `_substrate_brief` reads `$GT_CERT_DIR/brief.txt` READ-ONLY; missing dir / missing file / unreadable / EMPTY each `_adapter_fail(...)` under proof OR substrate `gt_agent.py:442-502`; `_generate_brief` defence-in-depth re-raises `HOST_BRIEF_FORBIDDEN` if host generation is ever reached under proof/substrate `:505-531`; legacy host path explicitly "unreachable in the leaderboard deepswe_full.yml run" `:513-516` and re-raises under strict flags `:544-557` | §6 (task-start brief), §13.3 (consume the ONE engine's brief) | CONFORMS |
| 3.2 | **Raise-with-[GT_META]-error visibility**: `_adapter_fail` PRINTS `[GT_META] gt_artifacts=…; error=DEEPSWE_ADAPTER_FAIL detail=<token>` then raises `:101-114` (the bug-#7 fix: pier can swallow the exception; the printed line is what the workflow grep + classifier see). Format cross-checked against `deepswe_outcome._gt_meta_witness` regex `([A-Za-z_]\w*)\s*=\s*([^;\|\s]+)` and the substring key `DEEPSWE_ADAPTER_FAIL in trial_log` — parses/classifies correctly; an infra token EMBEDDED in the detail can never flip class to INFRA (adapter-line exclusion, tested) | §12 (gates/certs reconcile vs runtime witness) | CONFORMS |
| 3.3 | **Consumption witness, fail-closed proof OR substrate**: `_emit_gt_meta_witness` resolves the consumed graph read-only, fingerprints via `proof.graph_edges_hash` (the SAME canonical hash), compares the LSP cert's `graph_hash_after_lsp`, prints the canonical witness line + suffix (`gt_prebuilt_active=true`, `hook_graph_hash_matches_post_lsp=<bool>`), and HARD-STOPS on: no-resolved-graph, empty hash, **hash mismatch** (`GRAPH_FAIL_HASH_MISMATCH`), and (proof only) **absent post-LSP hash** ("no unprovable consume") `:561-750`. The host-side resolution survives GT_PROOF_MODE=1 on the runner: `from_env` takes the proof branch (gdb='' — `context.py:106-109` ignores GT_HOST_GRAPH_DB there) but `resolved_db = ctx.graph_db or host_graph` `:640` rescues via the explicit env fallback — works, with a fragility note (gap G3) | §12 (graph_witness is the runtime truth; cert-FAIL reconciliation), §13.5 | CONFORMS (live match = **U6**) |
| 3.4 | **Ordering witness → brief → preamble → run** `:790-807`: graph authority is proven before the brief is consumed; either failure prints the classified line then raises pre-model-spend. Both orderings classify GT downstream — no masking seam found | §12 | CONFORMS |
| 3.5 | **delivered_instruction.txt**: the EXACT augmented instruction persisted to `/tmp/gt/delivered_instruction.txt` `:811-818`, collected by the workflow `:650` | AGENT-OBSERVATION rule (provable delivery) | CONFORMS (verbatim-in-trajectory = **U1**) |
| 3.6 | Substrate mode **removes the in-container graph build entirely** (no dual graph, no fallback) `:399-421`; `GT_BASELINE` strict `== "1"` parse `:66-68` | §13.3 (ONE graph), §7 | CONFORMS |

### Link 4 — Injection point 2: per-turn observations (`gt_mini_patch.py`)

| # | Behavior @ file:line | gt_gt clause | Status |
|---|---|---|---|
| 4.1 | Load path: `.pth` primary + `default.py` append + pyc purge + **build-time selftest** (`GT_SELFTEST_FAILED` fails the image build) `gt_agent.py:230-296,423-425` | §6 (hooked onto the agent) | CONFORMS (runtime load marker = **U3**) |
| 4.2 | **Exact-match pillars** (bug #1): `_norm_fp` + `file_path = ?` in `_top_func_names:449-455`, `_sibling_context:605-611`, `_graph_contract_block:958-967`, `_query_scope:699-707`, consensus `:789-796`, cochange `:1031-1036`. Residual: 3 functions (`_resolved_witnesses_for_file:485,508`, `_caller_contract_for_file:553`, `_edit_target_callee_contracts:650`) still use suffix-`LIKE '%'+nfp` — **verbatim parity with `v1r_brief.py:539,681,714`** (the canonical engine uses LIKE there too), so this is parity-by-design, not a regression (note G4) | ffc6c7dc "exact-match pillars"; §13.3 parity-with-v1r | CONFORMS (with G4 note) |
| 4.3 | **Verified-caller counts** (bug #2): `[CALLERS] n verified caller(s)` counts ONLY `_DETERMINISTIC_METHODS` edges + `confidence ≥ 0.7` + non-test; legacy schema without `resolution_method` **abstains** (count suppressed, never faked) `:929-957,999-1001` | §2.3 trust model; ffc6c7dc | CONFORMS |
| 4.4 | **Sanitizers** (bug #4): `_sanitize_signature` (pyright hover-markdown strip) `:302-327` applied at `:671,:969`; `_clip_balanced` (balanced-prefix clip, no dangling operator) `:331-384` applied to PRESERVE values `:986`; empty-after-repair ⇒ drop | ffc6c7dc | CONFORMS |
| 4.5 | **`_connect_ro` + one-time readability probe** (bug #5): `mode=ro` always, `immutable=1` ONLY in substrate/proof (the truly-ro mount), trivial schema SELECT on every open, ONE classified `[gt-patch] GRAPH_UNREADABLE_IN_CONTAINER:` line on first failure `:394-436` | ffc6c7dc ro/immutable | CONFORMS (live no-probe-error = **U5**) |
| 4.6 | **Dedup bounds**: per-(kind,relpath)-once `_seen` `:896-899`, body capped `lines[:6]` `:885`, contract per-file-once `:906,917`, cochange one-shot consumed only on real emit `:1051`, cmd history capped 12 `:1111-1113` — no unbounded spam surface | §6 / Cursor-mentality (no flooding) | CONFORMS |
| 4.7 | **L5 nudges**, once-each: repeated-command loop (≥4) + scaffold trap (≥25 actions, 0 source edits) `:1100-1124`; failure-persisted (same failure sig ×2 AFTER a source edit; reads the command's OWN output captured pre-GT-append, so it can't self-trigger) `:1127-1149` | §12 row L5 (nudge delivered at a live hook) | CONFORMS (live firing = **U11**) |
| 4.8 | **L6 gated OFF in substrate**: `_invalidate_on_edit` returns immediately when `_substrate_active()` `:1081-1082` (option (a): never mutate/fork the certified graph; preserves hook==post-LSP witness). **Deliberate divergence from gt_gt §6's "per edit: incremental reindex" row — the doc is mid-update per the prompt**; §12's L6 criterion ("preserved LSP enrichment") is exactly WHY it's off (a `-file` reindex strips LSP) | §12 row L6; §6 (mid-update) | CONFORMS-to-code (doc mid-update flagged; live absence = **U10**) |
| 4.9 | **Consensus gating**: Layer-A once on FIRST source-view, 1-hop graph scope, conf ≥ 0.5, exact-match, no "primary target" anointing `:759-827`; Layer-B in-scope reinforcement once-per-file + off-scope re-anchor after 3 strays `:719-756` | §6 (per-view), OH-parity per §13.4 | CONFORMS |
| 4.10 | **Categorical fact gate + stdlib-shadow guard everywhere**: `_DETERMINISTIC_METHODS` `:56-63` (name_match NEVER a fact, even cc≤1), `_is_stdlib_shadow` `:259-269`, re-checked **per row** in callee contracts `:663-664`; unverified hints labeled `(unverified)` `:577-583` | §2.3, §12, P0 stdlib-shadow closure | CONFORMS |
| 4.11 | Evidence transport: appended to `out["output"]` via the patched `execute()` `:1152-1211` — rendered into the model's observation per the attachment mapping `:8-14` | §6 (per-view/per-edit hooks) | **UNVERIFIED-NEEDS-RUNTIME** (**U2** — the render half lives in mini-swe-agent, external) |
| 4.12 | `_db_path` consume-or-quiet: GT_HOST_GRAPH_DB unconditional; in substrate/proof **never** falls back to `/tmp/graph.db` `:228-246` | §13.3 ONE graph | CONFORMS |

### Link 5 — Exit path (`deepswe_outcome.py` + workflow tail)

| # | Behavior @ file:line | gt_gt clause | Status |
|---|---|---|---|
| 5.1 | **Witness-missing → GT** (rule 3b): agent ran + reward<1 + `gt_prebuilt_active` unknown ⇒ class GT, **stays in the denominator** `deepswe_outcome.py:263-270`; precedence INFRA > GT > RESOLVED > GT-witness-absent > AGENT > UNKNOWN `:218-280`. Tests: `test_deepswe_outcome_classify.py` (29 cases incl. `test_classify_gt_missing_witness_ran_unresolved`) | §12 (unproven consumption = GT's problem) | CONFORMS |
| 5.2 | **Line-anchored INFRA** + adapter-line exclusion: markers count only at line start (modulo `::error::`); any line carrying `DEEPSWE_ADAPTER_FAIL` is never scanned for infra `:88-108`; `GT_ISSUE_MISSING` is in the INFRA set `:71-81`. Tested (embedded-token + line-anchored cases) | §12 reconcile discipline | CONFORMS |
| 5.3 | INFRA/UNKNOWN excluded from the resolved denominator; 8-dp tally; paired-delta scaffold with no fabricated baseline `:283-372` | CLAUDE.md 8-dp + paired-Wilcoxon mandates | CONFORMS |
| 5.4 | **Summarize parses the steps VALUE** (`AGENT_RAN_STEPS=[0-9]+` / `"n_agent_steps": N`, numeric-only; 0/absent/None = launch-fail) `deepswe_full.yml:706-721` — the P0.1-d green-zero-run kill | 9bf106ca | CONFORMS |
| 5.5 | **Upload `if: always()`** `:672-679`; Collect `if: always()` mirrors all 8 artifacts + per-language certs + delivered_instruction + deep metrics + provenance `:634-670` | §13.5 / CLAUDE.md deep-logging | CONFORMS |
| 5.6 | Witness-verify step (post-pier): fails on `error=DEEPSWE_ADAPTER_FAIL`, on no `gt_prebuilt_active=true` ("delivery, not telemetry"), and on `hook_graph_hash_matches_post_lsp=False` `:607-632`; gated `if: env.GT_PORTABLE_SUBSTRATE == '1'` (skipped only when the substrate step legitimately exited the transitional path) | §12, AGENT-OBSERVATION rule | CONFORMS |
| 5.7 | Classifier inputs on a REAL pier `jobs/` layout (glob shapes `jobs/*/*__*/result.json`, trajectory path, verifier stdout) | — | **UNVERIFIED-NEEDS-RUNTIME** (**U9**) |
| 5.8 | Classification visibility of substrate-step §E markers | — | **GAP G1** (see below) |

### Link 6 — Per-language behavior

| # | Behavior @ file:line | gt_gt clause | Status |
|---|---|---|---|
| 6.1 | **LSP is the ONLY per-language branch**: server choice via `src/groundtruth/lsp/config.py::LSP_SERVERS` (.py→pyright, .ts/.tsx/.js/.jsx→typescript-language-server, .go→gopls serve, .rs→rust-analyzer, .java→jdtls); `gt_run_proof._baked_lsp_problems` derives the baked-set check FROM config (not a benchmark list) `:246-270`; per-language resolve loop + aggregation `:575-636` | ONE-PRODUCT rule ("LSP is one surface") | CONFORMS |
| 6.2 | Per-turn pillars are **pure SQL over the tree-sitter graph — zero language branches** in `gt_mini_patch.py` (grep: no language/extension conditionals beyond the `_SRC_EXT` membership list, which covers all 5 DeepSWE languages + more `:113-116`) | §13.4 language-agnostic mandate; retire-ast | CONFORMS |
| 6.3 | Known data (not code) asymmetries, all honest per gt_gt: property richness thinner on Go/Rust (§2.5), relationship edges JS/TS-favored (§2.5), PRESERVE lines correspondingly sparser — correct-or-quiet where absent `:975-991` | §2.5 / §10 | CONFORMS |
| 6.4 | `_STDLIB_MODULES` shadow list is the **Python** stdlib set applied to all languages `:67-75` — strictly suppress-only (can only DROP a witness, never invent one), so the failure direction is silence on Go/JS calls through identically-named modules (`os.`, `time.`, `io.`) | Cursor-mentality (wrong>silent) | CONFORMS (note G5) |
| 6.5 | Real per-language run: gopls launch fix (`8ae5584d`), rust-analyzer/tsserver warm-up, per-language `LSP_ACTIVE_VALID` certs | §3/§7 | **UNVERIFIED-NEEDS-RUNTIME** (**U7**) |

---

## (b) NEW gaps post-fix (adversarial pass on the seams the fixes created)

None is a P0; ordered by impact.

**G1 — Substrate-step §E markers don't reach the classifier (classification-fidelity gap).**
The substrate-proof step's fail-closed echoes — `GT_SUBSTRATE_DIGEST_MISSING`,
`GT_SUBSTRATE_PULL_FAIL`, `GT_RUN_PROOF_FAIL`, `GT_ARTIFACT_MISSING` (`deepswe_full.yml:378,
414,490,503`) — go to the GHA step log only; **only `GT_ISSUE_MISSING` is teed into
`trial_output.log`** (`:443`). When that step fails, pier never runs, `trial_output.log`
usually doesn't exist, and Collect's `deepswe_outcome.py` (if:always) classifies the task
**UNKNOWN instead of INFRA**. Containment: the job is still red (step exit 1), UNKNOWN is
denominator-excluded like INFRA, and summarize counts it as launch-fail — so the rates stay
honest; only the per-task `failure_class` label is degraded. Fix shape: `| tee -a
trial_output.log` on the four echoes (the GT_ISSUE_MISSING pattern, already proven).

**G2 — Double `<gt-task-brief>` wrapping (cosmetic).**
`generate_v1r_brief`'s `brief_text` already begins with `<gt-task-brief>`
(`v1r_brief.py:1417`); `emit_brief` writes it verbatim to brief.txt; `gt_agent.run` then wraps
the consumed text in a SECOND `<gt-task-brief>` (`gt_agent.py:797-799`). The delivered
instruction nests the tag. Harmless to the model, but any audit/tooling that counts tag
occurrences (incl. a naive D2 grep) will see 2 opens — count blocks, not tags.

**G3 — Witness graph-resolution survives proof mode only via an env fallback (fragility).**
`_emit_gt_meta_witness`'s comment claims the "non-proof host-handoff branch" of
`GTRuntimeContext.from_env` resolves GT_HOST_GRAPH_DB — but the host runner HAS
`GT_PROOF_MODE=1` (workflow top-level env), so `from_env` takes the PROOF branch and returns
`graph_db=''` (`context.py:106-109` reads only GT_GRAPH_DB there). The witness works solely
because of the explicit `resolved_db = ctx.graph_db or host_graph` (`gt_agent.py:640`). If
that `or host_graph` is ever refactored away, every proof-mode task fails the witness. Stale
comment + single-point fallback — worth a hardening test (`monkeypatch GT_PROOF_MODE=1` +
GT_HOST_GRAPH_DB-only ⇒ witness resolves).

**G4 — Residual suffix-LIKE in 3 witness/contract queries (parity-by-design, residual risk).**
`_resolved_witnesses_for_file`, `_caller_contract_for_file`, `_edit_target_callee_contracts`
match `file_path LIKE '%' + norm_relpath` (`gt_mini_patch.py:485,508,553,650`) — verbatim
parity with `v1r_brief.py:539,681,714`, so the ffc6c7dc "exact-match pillars" claim is
accurate for the five pillars it names but NOT a property of the whole patch. A nested repo
where `a/src/util.py` and `vendor/a/src/util.py` coexist can still cross-attribute witnesses.
If this is ever fixed, fix it in v1r_brief FIRST and re-port (ONE-PRODUCT parity).

**G5 — Stdlib-shadow list is Python's, applied to all 5 languages (suppress-only).**
`_STDLIB_MODULES` (`os, json, time, io, …`) guards every language's witnesses. Direction is
safe (it can only suppress), but Go/JS calls through same-named real project modules (`os.`,
`time.`) lose legitimate witnesses. A per-language stdlib set (or a graph-presence check:
suppress only when no project node matches the qualifier) would recover them.

**G6 — Absent-post-LSP-hash is fail-closed in PROOF mode only.**
`_emit_gt_meta_witness` hard-stops on a missing `graph_hash_after_lsp` only `if proof`
(`gt_agent.py:738-745`); in substrate-active-but-non-proof mode an absent hash passes the
adapter (the printed line shows `hook_graph_hash_matches_post_lsp=False`, which the
workflow's grep — but only this workflow's — converts to a failure, and the classifier
converts to GT). In `deepswe_full.yml` proof=1 always, so covered; the asymmetry only bites
a future substrate-without-proof harness. The docstring ("raise scope CONSISTENT… proof OR
substrate") slightly overstates the code.

---

## (c) The D2 trajectory verification checklist — 12 UNVERIFIED-NEEDS-RUNTIME items

Per the AGENT-OBSERVATION rule: every item below is proven ONLY by Reading the raw per-task
run artifacts (`trial_output.log`, `jobs/<ts>/<task>__*/agent/mini-swe-agent.trajectory.json`,
`trial_results/gt_artifacts/*`, `delivered_instruction.txt`) of a REAL run — chronological
read, never grep-only. Run this **once per language** (python, go, rust, typescript,
javascript) — the per-language column is the §13.4 generalization proof. Ordered to match the
chain; each U-item maps to its table row.

| # | What to READ, in order | Proves (link) |
|---|---|---|
| **U6** | `trial_output.log`: find the `[GT_META] graph_witness …` line BEFORE the first agent step. Assert `gt_prebuilt_active=true`, `hook_graph_hash_matches_post_lsp=True`, `graph_hash_after_lsp != (absent)`, `substrate_digest` == the pinned input. Assert NO `error=DEEPSWE_ADAPTER_FAIL` anywhere | witness consume == post-LSP graph (3.3) |
| **U8** | `trial_results/gt_artifacts/brief.txt`: non-empty; contains `<gt-localization` + `<gt-task-brief>` + file candidates that exist in THIS repo; `gt_issue_anchors.json` present. Sanity: no gold/test leakage strings | substrate brief real per language (1.2) |
| **U1** | `trial_results/delivered_instruction.txt`: starts with `<gt-task-brief>` wrapping EXACTLY the bytes of brief.txt (note G2: expect the doubled tag), ends with the `## GroundTruth codebase intelligence (automatic)` preamble. Then open the trajectory's FIRST user/instruction message and assert it equals delivered_instruction.txt **verbatim** (this is the `{{task}}` injection through pier) | brief verbatim in --task (3.4/3.5) |
| **U3** | Trajectory, first observation: the one-time `[gt-patch:loaded]` marker present (`gt_mini_patch.py:1158-1160`). Absent marker = the .pth never loaded → every per-turn claim below is void | patch loaded in-container (4.1) |
| **U4** | Trajectory: any `<gt-evidence>`/`<gt-contract>` content that can ONLY come from `/gt_artifacts/graph.db` (e.g. a `[WITNESS] … called by -> <cross-file>:<line>` matching the substrate graph) proves the `--ae` env (GT_HOST_GRAPH_DB et al.) reached the agent process. Optional direct probe: an early agent `env \| grep GT_` command output in an observation | env reached the container (2.3) |
| **U5** | Trajectory + trial_output.log: assert ZERO occurrences of `[gt-patch] GRAPH_UNREADABLE_IN_CONTAINER:` (the one-shot probe error). Presence = the ro/immutable connect failed and every pillar was silently dead | ro+immutable connect live (4.5) |
| **U2** | Chronological read of EVERY observation: per source-VIEW expect (first view) `<gt-scope files=…>` then per-file-once `<gt-evidence kind="post_view" …>` with `[WITNESS]`/`[CALLERS]`/`[SIBLINGS]`; per source-EDIT expect `<gt-contract>` (`[SIGNATURE]`, gated `[CALLERS] n verified caller(s)`, `PRESERVE`/`[RAISES]`/`[RETURNS]`), first-edit `<gt-cochange>`, and `<gt-evidence kind="post_edit">` with `[CALLEE]` lines. Verify CORRECTNESS per gt_gt's verification protocol: every `[WITNESS]`/`[CALLEE]` claim must match the actual repo (open the named file:line); every count must be ≥ the visible deterministic edges — delivered ≠ correct | per-turn pillars delivered + correct (4.2–4.4, 4.9–4.11) |
| **U11** | Same chronological read: L5 nudges (`<gt-nudge reason="loop\|scaffold_trap\|failure_persisted">`) fire AT MOST once each, only when their event actually occurred (count the repeated commands / actions-without-edit yourself — §12: an event hook's absence ≠ dead layer); consensus re-anchor only after ≥3 off-scope views. Then CONSUMED: did the agent's next thought/action reference the nudge/scope? | L5/consensus event-correct + consumed (4.7/4.9, §12) |
| **U10** | Trajectory: ZERO `gt-index -file` invocations and zero writes under `/gt_artifacts` (L6 gated off in substrate — deliberate); cross-check `trial_results/graph.db` sha256 == `run_manifest.graph_db_sha256` (the graph never mutated mid-run) | L6 off + graph immutability (4.8) |
| **U7** | `trial_results/gt_artifacts/lsp_certificate_<lang>.json` for EVERY language the manifest's `language_distribution` lists with a server: verdict `LSP_ACTIVE_VALID` (or `LSP_NO_OP_VALID_WITH_WARM_SERVER`), never `LSP_FAIL_NO_WARM`/`LSP_INSTALL_MISSING`; on the Go task specifically this is the gopls-launch-fix live proof. Cross-check `gt_lsp_metrics.txt` resolved>0 on the dominant language | per-language LSP live (1.5, 6.5) |
| **U12** | `trial_results/gt_artifacts/embedder_certificate.json`: verdict OK, model identity == the CONFIGURED default (gte-modernbert-base per the §5 mid-update; `discrimination_margin` > 0), `run_manifest.cert_versions` all non-null | embedder real + consistent (1.6) |
| **U9** | Collect-step output (or rerun `python3 scripts/verify/deepswe_outcome.py jobs` on the artifact): `AGENT_RAN_STEPS=<n>0>`, `FAILURE_CLASS` printed and CORRECT for what you just read (RESOLVED/AGENT/GT — reconcile manually against U6's witness per §12's reconcile rule); verifier tally parsed | exit-path classification on real layout (5.1–5.7) |

**Success criterion for D2:** all 12 items pass on at least one trajectory per language
(5 languages × 12 checks), with item U2's correctness sub-check (delivered ≠ correct) applied
to every GT block encountered — only then may any layer be reported above "delivered;
correctness unverified" per gt_gt's VERDICT GATE.

---

*Audit complete: read-only; no code changed; no commit. Audited files at HEAD `0e2489cc`:
`scripts/swebench/gt_run_proof.py`, `.github/workflows/deepswe_full.yml`,
`artifact_deepswe/gt_agent.py`, `artifact_deepswe/gt_mini_patch.py`,
`scripts/verify/deepswe_outcome.py`, `artifact_deepswe/gt_integration/deepswe_gt_pier.yaml`,
plus cross-reads of `src/groundtruth/runtime/{context,proof}.py`,
`src/groundtruth/lsp/config.py`, `src/groundtruth/pretask/v1r_brief.py`,
`scripts/metrics/graph_certificate.py`, and the fail-closed test suites
(`tests/fail_closed/test_portable_substrate.py`, `test_deepswe_outcome_classify.py`).*
