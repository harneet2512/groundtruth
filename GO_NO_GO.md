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
| Stage 0 — map final runtime path | ✅ committed | `e593f72e` |
| Stage 1 — LSP liveness cert | ✅ local gate proven (15 tests) | `1c3ec178` |
| Stage 2 — graph handoff cert | ✅ local gate proven (18 tests) | `dbc41e43` |
| Stage 3 — embedder usage cert | ✅ local gate proven (15 tests) | `061e50bb` |
| Stage 4 — LSP+gates in-container | ⚠️ escalation checkpoint (NOT final) — runtime boundary proven, workflow surfaced the provisioning tension | `b88beeec` |
| Stage 4.1 — runtime strategy decision + proof-leak fix | ✅ Option B chosen; host run_v74 leak CLOSED; rule reframed (15 tests) — awaiting review | this commit |
| Stage 5 — image cache + manifest | ⛔ not started | — |

## Blockers (from Stage 0 UNKNOWNs)

- **U1** ✅ CLOSED in code (Stage 1): LSP-liveness certificate + warm probe + 7-verdict gate; a
  `residual==0` pass now requires `lsp_warm=true`. Live warm-probe proof pending Stage B/C run.
- **U2** ✅ CLOSED in code (Stage 2): graph certificate (depth + FTS5 MATCH + handoff), canonical
  cross-stage edge hash + drift test, and the `[GT_META] graph_witness` hook emitter. Live
  cross-stage hash equality (`hook_graph_hash == graph_hash_after_lsp`) asserted in Stage C.
- **U3** ✅ CLOSED in code (Stage 3): `assert_same_embedder_identity` now wired into `run_v74` +
  `localize` (was never called); embedder certificate (identity + consumption + all-zero/dropped)
  + `classify_embedder` hard gates. Live cross-path identity equality asserted in Stage B/C.
- **U4** ✅ CLOSED in code (Stage 4): runtime `assert_container_boundary` fails-closed
  `FINAL_PIPELINE_HOST_SPLIT_FAIL` on host+proof; workflow moves LSP + gates into `gtsrc` via
  `docker exec` (8 flags), forbids substrate under proof, agent gets proof env. In-container
  provisioning is GHA-proven Stage B (see PIPELINE_PROOF_REPORT escalation).
- **U5** GHCR-only pull + image-cache manifest + `gt_commit` pinning unproven; cache≠run set (Stage 5).
- **U6** whether the host path invokes `GTRuntimeContext`/`runtime.preflight` consistently (Stage 0 UNKNOWN — revisit).

## Next allowed action

**NOT Stage 5.** Option B (unified GT substrate runtime) is chosen but **not yet wired** — image-
cache work (Stage 5) presupposes the final runtime path. Next is either: wire the unified-substrate
runtime (same `foundational_gates.py`/`resolve.py`/certs, baked GT image + shared source/volume,
host orchestrates only), OR a Stage-B 1-task run to test whether Option A provisioning is
deterministic + cheap. Stage 5 begins only after the runtime path is final. No GHA runs until the
local staged gates (Phase 6 Stage A) pass for the final runtime path.
