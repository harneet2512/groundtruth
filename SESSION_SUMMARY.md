# Session Summary

## Date / Time
2026-06-03

## Branch
gt-consensus-curation

## Commit
HEAD `ed438843` (16-commit chain from `51de7275`). Nothing pushed.

## Objective
Two halves: (1) analyze the 30-task run `26909714974` trajectories and root-cause why GT
produced ~0 useful flips; (2) make the benchmark infrastructure **legitimate, fail-loud,
parallel, and install-once** so the next paid run can be trusted.

## Files read (evidence)
- 25 task trajectories in `.tmp_30full/` (output.jsonl) + `gt_debug/full_run.log` per task
- `graph_localizer.py` (`_get_embedder`, weights, `_path_decay_scores`), `v7_4_brief.py`
  (`_get_model`), `v1r_brief.py` (`_localization_header`), `edge_verifier.py` (`start`,
  `verify_caller`), `gt-index/.../sqlite.go` + `main.go` (FTS5), `parser.go` (data_flow),
  `preflight_pipeline.py`, `setup-eval/action.yml`, `Dockerfile.eval-runner`, BRIEFING.md

## Key findings (from the 30-task analysis)
- **1/25 resolved** (sh-744, via the agent's own grep — GT's brief headline was wrong).
- GT uniquely steered the agent to a gold file in ~9 tasks; **all 9 still failed** downstream
  (5 missed a co-required file, 4 right-file-wrong-logic). Delivery-correct != flips.
- **The run was a CRIPPLED pipeline** — confirmed from run logs, not telemetry:
  FTS5 index missing 25/25 (built without `-tags sqlite_fts5` -> Python rebuild), semantic
  DEAD (onnxruntime + model absent -> W_SEM=0 both halves), LSP 0 real verifications (pyright
  absent -> 0ms confidence-filter stamps). So every localization-quality conclusion is
  CONFOUNDED and must be re-measured on a provisioned run.
- Localizer hardcodes hop depth (`max_hop=3`, `beta=0.85`) — flagged vs the Dynamic pillar.

## Implementation changes (16 commits)
- **No-silent-fallback gates**: FTS5 (`GT_REQUIRE_FTS5` Go gate + build tag everywhere),
  embeddings (force-ONNX both halves + `GT_REQUIRE_EMBEDDER` raise), LSP (real launch via
  `start(warm=True)` + per-task resolve probe asserting `lsp_references`+latency>0).
- **Behavioral preflight** `preflight_full_stack.py` (probes real non-zero results) + the
  DeepSWE `preflight_pipeline.py` made HARD (was advisory) + new `check_data_flow` + strict
  Go-built FTS5.
- **Per-task graph-base dimension gate** (OH parity with DeepSWE, one shared source).
- **Legitimacy**: `GT_FORBID_PREBUILT_GRAPH=1` forces fresh in-container indexing on 300 +
  DeepSWE; preflight legit-check fails on contradictory config.
- **Parallelize**: `deepswe_full.yml` = 113-task matrix (was single-task); capped all
  matrices at the real ~20 runner ceiling.
- **Install-once**: corrected the baked eval image (fts5, Go 1.23, pier, docker CLI,
  GT_MODELS_ROOT, GT_EVAL_IMAGE) and wired BOTH main workflows to run `container:` it.

## Metrics before / after
- Before: 1/25 resolved on a degraded pipeline (confounded). No valid quality metric.
- After: code-level gates verified (FTS5/embedder/LSP gates RAISE on degradation; graph-dim
  gate PASSES 7 dims + FAILS data_flow on a stale-binary db). No new RUN yet — metrics are
  pending the provisioned, gated run.

## Tests / runs executed
- Local: py_compile all edited files; embedder fail-loud RAISES (both halves); preflight
  passes permissive / aborts required; legit-check FAILs on forbid+armed; graph-dim gate
  PASS/FAIL behavioral test on real graph.db. Go changes compile in CI (no Go/GCC locally).
- **No GHA run yet** — the container/DinD wiring is UNVALIDATED; must run 1 task each first.

## Result
Benchmark infra is legitimate + fail-loud + parallel + install-once. Operational steps in
`BENCHMARK_RUNBOOK.md`. The 30-task quality verdict is retracted as confounded.

## Regressions
None observed. Pre-existing Pyright noise only. Bare-runner workflows (canary) unaffected by
the `GT_EVAL_IMAGE` gating.

## Rollback decision
All reversible: `git revert ed438843 cc2bd22a 61db3b13 b0f957d7 25d6eb50 e56784f6 db867327
d879a1c2 bf8fd2b9 81eff53b 51de7275` (+ the doc/delivery commits). Nothing pushed.

## Open blockers
1. **Container/DinD wiring UNVALIDATED on GHA** — validate 1 task each before the paid 113/300.
2. DeepSWE task-image `docker pull` has no GHCR cache (the last bottleneck).

## Next allowed action
Dispatch `build_eval_image.yml`, then a 1-task validation on `deepswe_full` (max_tasks=1) and
the OH 300 (limit 1). Only if green -> the full sets. Then re-measure localization quality on
the now-provisioned pipeline (the 30-task verdict was confounded).
