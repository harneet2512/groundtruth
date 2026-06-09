# GO / NO-GO — Full-Runtime Structural-Legitimacy Proof

> Running state of whether we may launch GHA runs. Updated after each stage. Branch `gt-trial`.
> Baseline `2a4f965a`. Discipline contract: `docs/SWE_LIVE_LITE_OH_GT_INTEGRATION_HANDOFF.md`.

## Current verdict (after Stage 0)

| Question | Verdict | Why |
|---|---|---|
| Can we run **300 dry / gates_only**? | **NO** | Stages 1–5 not implemented: no LSP-liveness cert, no graph-handoff cert, no embedder-usage cert, LSP+gates still on host, cache≠run set. |
| Can we run **30 live**? | **NO** | Depends on 300-dry structurally passing first. |
| Can we run **full 300 live**? | **NO** | Depends on Stages 1–5 + staged validation A–E. |

## Stage status

| Stage | State | Commit |
|---|---|---|
| Baseline | ✅ clean tree | `2a4f965a` |
| Handoff doc + discipline | ✅ committed | `9f0a7d83` |
| Stage 0 — map final runtime path | ✅ this commit | `audit: map final GT runtime path` |
| Stage 1 — LSP liveness cert | ⛔ not started (safe to begin after review) | — |
| Stage 2 — graph handoff cert | ⛔ not started | — |
| Stage 3 — embedder usage cert | ⛔ not started | — |
| Stage 4 — LSP+gates in-container | ⛔ not started | — |
| Stage 5 — image cache + manifest | ⛔ not started | — |

## Blockers (from Stage 0 UNKNOWNs)

- **U1** LSP has no warm certificate → `residual==0` is a vacuous pass (Stage 1).
- **U2** hooks' graph hash vs post-LSP graph hash unproven on a live run (Stage 2).
- **U3** embedder model-root identity across `run_v74`/`localize` unproven; `assert_same_embedder_identity` never called (Stage 3).
- **U4** agent step sets no `GT_PROOF_MODE`/`GT_CONTAINERIZED`; LSP+gates run on host (Stage 4).
- **U5** GHCR-only pull + image-cache manifest + `gt_commit` pinning unproven; cache≠run set (Stage 5).
- **U6** whether the host path invokes `GTRuntimeContext`/`runtime.preflight` consistently (Stage 0 UNKNOWN — revisit).

## Next allowed action

**Stage 1 — LSP-liveness certificate** (after user review of Stage 0). No GHA runs until the
local staged gates (Phase 6 Stage A) pass.
