# Session Summary

## Date / Time
2026-06-08

## Branch
`gt-trial`

## Commit
`0ea41e8b` — feat(proof-mode): fail-closed runtime surface (Stages 1-3 core) — proof.py + wired guards
(parent `f8b96c79`). Remotes rule: push code to **origin (harneet2512)** ONLY; hbali-stack is
run-infra (403), never push code there.

## Objective
Implement the "Make GT Full-Runtime Mandatory" plan (proof-mode fail-closed across code,
container, and the final-300 pipeline), proving each flagged bug against the real code
before gating it, then verify via a 10-task probe and update docs.

## Files read (evidence, not assumed)
| Claim | File | Lines | What it proved |
|---|---|---:|---|
| FTS5 falls back to python-side creation | graph_localizer.py | 258-377 | REAL — opens own writable conn, `_FTS5_CREATE/_POPULATE`, mutates graph.db on a read path |
| run_v74 semantic zeroed despite present embedder | v7_4_brief.py | 481,509-522,730-745,960,1049-1055 | REAL — ablation A/B*, RRF det/nosem, sparse weights zero W_SEM; availability≠usage |
| embedder ABSENCE already raises | v7_4_brief.py:353; graph_localizer.py:1361 | — | availability enforced; USAGE was not |
| context.from_env accepts GT_HOST_* aliases | context.py | 68-69 | REAL (finding A1) |
| closure rebuild best-effort | resolve.py | 838-879 | REAL — silent return if binary absent, logs+continues on failure |
| `_semantic_score_by_file` swallows to {} | graph_localizer.py | 1371-1409 | REAL — DB error / no docs / encode exception; no all-zero guard |
| GT_META stdout leak (probe memory) | graph_localizer.py | 296-376 | NOT FOUND — all L1 diagnostics are `file=sys.stderr`; claim UNVERIFIED |
| LSP "hardcoded python" | resolve.py | 66-119 | MIS-SCOPED — resolve.py is multi-lang/self-guarding; only the workflow passes `--lang python` |
| Workflow proof flags | swebench_300task.yml | 56-64, 424-449 | 6/8 armed; GT_PROOF_MODE + GT_CONTAINERIZED ABSENT (C7); gt_use_substrate_image default false (B1) |

## Research checked
BRIEFING.md (hook-required): §4 "closure freshness is for impact/trace, NOT brief ranking;
lever = content + hub-demotion + confidence-gate, NOT reach" — confirmed no guard changes a
weight. gt_trial §1/§1.5/§3.1/§4. CLAUDE.md ONE-PRODUCT-RULE, LIPI, DEFINITION OF DONE.

## Implementation changes (committed `0ea41e8b`)
- **NEW `src/groundtruth/runtime/proof.py`** — the ONE proof-mode surface: `is_proof_mode`/
  `require` (raise in proof, warn outside), `reject_host_aliases`, `assert_fts5_native`,
  graph-meta stamp/read, `stamp_lsp`/`stamp_closure`/`assert_closure_after_lsp`/
  `assert_lsp_before_scoring`, `forbid_no_sem_config`/`assert_semantic_consumed`, `context_id`,
  `embedder_identity`. Byte-identical outside GT_PROOF_MODE=1.
- **context.py** — proof rejects GT_HOST_* (A1); unified `GTProofModeError`; `context_id` in artifacts.
- **graph_localizer.py** — `_fts5_candidates` forbids python-side FTS5 in proof (Stage 2);
  `_semantic_score_by_file` raises on the silent {} swallows in proof (Stage 3).
- **v7_4_brief.run_v74** — entry `forbid_no_sem_config`, exit `assert_semantic_consumed` (Stage 3).
- **resolve.py** — `stamp_lsp`/`stamp_closure`; `_rebuild_closure` + `assert_closure_after_lsp`
  fail-closed in proof (Stage 2; substrate-integrity, NOT ranking per BRIEFING §4).
- **tests/fail_closed/test_proof_surface.py** — 27 tests, two-sided (fail-closed in proof / inert out).

## Metrics before / after
- fail_closed suite: 0 → **27 passing**.
- Regression sweep (edited-module keys): **592 passed, 7 failed**; all 7 reproduce on baseline
  (verified via `git stash`) → **zero regressions**.

## Tests / runs executed
Local pytest only. No GHA/live run yet (gated — see Open blockers).

## Result
Stages 1-3 (the **GT code / architecture** plan section — the product-side bugs) implemented,
verified against code, locally tested, zero-regression, committed.

## Regressions
None (proven via baseline stash comparison).

## Rollback decision
`git revert 0ea41e8b` (single isolated commit).

## Open blockers (Stages 4-5 + the run)
1. Stage 4 (container boundary): oh_gt_full_wrapper.py host-fallback-fatal-in-proof,
   docker/gt-substrate-run.sh 8-flag arming, LSP-per-language. NOT started.
2. Stage 5 (pipeline): swebench_300task.yml GT_PROOF_MODE+GT_CONTAINERIZED on substrate exec (C7),
   GHCR-only-in-proof, gates-only==live, manifest+digests. NOT started.
3. The 10-task proof probe is gated on Stage 4: GT_PROOF_MODE=1 on the HOST path fails-closed by
   design — the probe MUST route through the substrate (gt_use_substrate_image=true).
4. Remote/auth: code → origin (harneet2512); the 300-task workflow runs on run-infra (hbali-stack,
   403 to push code). Which ref the run checks out is a user decision (remote discipline).

## Next allowed action
Resolve the two run decisions (remote target; finish Stage-4 substrate wiring before a proof probe,
or run a non-proof gates-only regression probe now), then Stage 4/5 + push + trigger + ngrok-SSE
watch + §4 ledgers.
