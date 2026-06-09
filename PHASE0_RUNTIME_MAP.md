# PHASE 0 — Final GT Runtime Path Map (Stage 0 audit)

> **Scope:** the **HOST mode** pipeline (`gt_use_substrate_image=false`, the default the real 300
> run uses) in `.github/workflows/swebench_300task.yml`. Substrate mode is out of scope (to be
> **forbidden under `GT_PROOF_MODE`** in Stage 4). Branch `gt-trial`, baseline `2a4f965a`.
>
> **No behavior changes in this stage** — this maps the current path *before* it is changed.
> Line numbers are approximate (`~L`) and may drift; cells are **UNKNOWN** where not yet verified
> by execution (per the rule: do not pretend).

## Legend
- **in-container?** = does this step execute inside the eval task container, or on the GHA host runner?
- **proof-fatal?** = does it hard-fail under `GT_PROOF_MODE=1`?
- **host-fallback?** = is a host-side / degraded fallback reachable on this step?

## Step-by-step map (host mode)

| # | Step | File / function | in-container? | uses GTRuntimeContext? | source_root | graph_db | models_root | proof-fatal? | host-fallback? | gates_only == live? |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Workflow dispatch / prepare | `swebench_300task.yml` prepare job | host runner | n/a | n/a | n/a | n/a | partial (asset fetch fatal) | n/a | yes (prepare shared) |
| 2 | Run task set source | `benchmarks/data/swebench_live_lite.jsonl` (~L141–176) | host | n/a | n/a | n/a | n/a | no | n/a | yes |
| 2b | Image cache set source | `live_lite_cache_images.yml` ← `benchmarks/live_lite_300_ids.json` | host | n/a | n/a | n/a | n/a | no | **DIFFERENT FILE → cache≠run risk (Stage 5)** | n/a |
| 3 | Image select / GHCR pull | preverify + pull (~L650–725); digests recorded (~L723) | host | n/a | n/a | n/a | n/a | UNKNOWN (Docker Hub fallback?) | **UNKNOWN — verify GHCR-only (Stage 5)** | yes |
| 4 | Start eval container `gtsrc` | `docker run … gtsrc` (Point A host, ~L815+) | container starts | n/a | `$ROOT` (container) | n/a | n/a | no | n/a | yes |
| 5 | Runtime preflight | `runtime/preflight.py` + `context.GTRuntimeContext.from_env` | **UNKNOWN** (host path may not invoke it; substrate path does) | **UNKNOWN** | UNKNOWN | UNKNOWN | UNKNOWN | yes (when run) | UNKNOWN | UNKNOWN |
| 6 | Graph build (`gt-index`) | `docker exec gtsrc "$IMG_GT" -root=$ROOT -output=/tmp/graph.db` (~L868) | **container** ✅ | no (Go binary) | `$ROOT` (container) | `/tmp/graph.db` (container) → copied to `/tmp/gt/graph.db` (host) | n/a | yes (`IDX_RC`!=0 fatal; FTS5 abort) | no | yes |
| 7 | FTS5 probe | `gt-index` index-time abort (`GT_REQUIRE_FTS5`) + `proof.assert_fts5_native` (called from `graph_localizer`) | container (index) / host (localizer) | partial | — | `/tmp/gt/graph.db` | n/a | yes | no | yes |
| 8 | **LSP enrichment** | `python -m groundtruth.resolve --resolve --lang <detected>` (~L1003) | **HOST** ❌ (→ move in-container, Stage 4) | UNKNOWN | `/tmp/gt/src` (host copy) | `/tmp/gt/graph.db` (host) | n/a | yes (resolve rc fatal in proof) | **YES (runs on host)** | yes |
| 9 | Warm probe | `lsp/client.py:probe_ready` **EXISTS, NEVER CALLED**; only `initialize`+`wait_for_progress` | with #8 | n/a | — | — | n/a | **no warm cert today → vacuous residual==0 pass (Stage 1)** | — | yes |
| 10 | Closure rebuild after LSP | `resolve.py:_rebuild_closure` (~L838) + `proof.stamp_closure`/`assert_closure_after_lsp` | with #8 (host) | n/a | — | `/tmp/gt/graph.db` | n/a | yes (rebuild fail/stale fatal in proof) | — | yes |
| 11 | run_v74 / localize / v1r (brief) | `v7_4_brief.run_v74` (~L740), `graph_localizer.localize` (~L1376), `v1r_brief` | **HOST** ❌ (wrapper on host, host-primary brief reads `GT_HOST_GRAPH_DB`) | UNKNOWN | `GT_HOST_SRC_ROOT=/tmp/gt/src` | `GT_HOST_GRAPH_DB=/tmp/gt/graph.db` | **UNKNOWN (baked `/opt/gt/models` vs host?)** | partial (`assert_semantic_consumed`, `forbid_no_sem_config`) | **UNKNOWN** | yes |
| 12 | Foundational gates | `scripts/metrics/foundational_gates.py` (~L1051, 3-GATE) | **HOST** ❌ (→ move in-container, Stage 4) | no | `/tmp/gt/src` (host) | `/tmp/gt/graph.db` (host) | reads via gate_embedder | yes (exit!=0 aborts; deliver-always softens quality axes) | **YES (runs on host)** | yes |
| 13 | Agent hook path | `oh_gt_full_wrapper.py` hooks L3/L3b/L4/L6 → `docker exec … --db=config.graph_db` | **container** ✅ | partial (`__post_init__`) | container `$ROOT` | `config.graph_db` (now = uploaded resolved graph via `_promoted_graph_db_path` honoring `GT_HOST_GRAPH_DB`) | UNKNOWN | agent step **does NOT set `GT_PROOF_MODE`/`GT_CONTAINERIZED`** | UNKNOWN (host fallbacks exist) | **agent skipped when gates_only=true** (L1200) |
| 14 | Artifact upload | upload-artifact steps `if: always()` (gate-deep, contracts, results) | host | n/a | n/a | n/a | n/a | n/a | n/a | yes (always) |

## Confirmed findings + resolution

1. **gates_only vs live SHARE the runtime/graph path.** `gates_only` only skips the paid agent
   (`if: inputs.gates_only != 'true'`, ~L1200) and the reaction joiner. Steps 1–12 are identical.
   → **No P0 stop-condition block on this axis.** (Stop-condition satisfied.)
2. **substrate vs host run different gate code** (`gt-substrate-run.sh` shell vs `foundational_gates.py`
   python). → **Decision: HOST mode only**; substrate **forbidden under `GT_PROOF_MODE`** in Stage 4.
   `foundational_gates.py` is the single gate.
3. **cache-set ≠ run-set source files** (`live_lite_300_ids.json` vs `swebench_live_lite.jsonl`). →
   reconciled in **Stage 5** (byte-identical ID-set check; one source of truth).

## Host/image split (the Phase-4 target)

Today, host mode **builds the graph in-container** (step 6) but runs **LSP (step 8) and gates
(step 12) on the host runner**, against a host copy of the source/graph (`/tmp/gt/src`,
`/tmp/gt/graph.db`). The agent hooks (step 13) run **in-container** against `config.graph_db`. This
is the host/image split. **Stage 4** moves LSP + gates into the container (`docker exec gtsrc …`)
so build/LSP/embedder/gates/hooks all operate on the **same in-container graph**, and forbids
substrate mode under proof.

## UNKNOWNs to resolve in later stages (do not pretend resolved)

- **U1 (Stage 1):** LSP has **no warm certificate** — `probe_ready` is never called, so a
  `residual==0` pass is vacuous. No `lsp_warm`/probe-latency/graph-hash fields exist.
- **U2 (Stage 2):** Is the hooks' `config.graph_db` hash **identical** to the post-LSP
  `/tmp/gt/graph.db` hash on a live run? (The `_promoted_graph_db_path` fix should make it so;
  unproven until a run emits the witness.)
- **U3 (Stage 3):** Does the host-mode brief/embedder load the **baked `/opt/gt/models`** ONNX or a
  host model (`GT_MODELS_ROOT`)? Do `run_v74` and `localize` share one model root? (`embedder_identity`
  exists; `assert_same_embedder_identity` is **never called**.)
- **U4 (Stage 4):** The **agent step sets no `GT_PROOF_MODE`/`GT_CONTAINERIZED`** (~L1200) — the
  wrapper/hooks are not in proof mode. Whether host-fallbacks in the wrapper are reachable in proof.
- **U5 (Stage 5):** Is the final pull **GHCR-only** in proof (no Docker Hub fallback)? Is there an
  image-cache manifest with digests? Is `gt_commit` pinned across jobs?
- **U6 (step 5):** Does the host path invoke `GTRuntimeContext`/`runtime.preflight` at all, and with
  the same `source_root`/`graph_db`/`models_root` as later steps?

## Stop-condition assessment (Stage 0)

- gates_only and live **share** the runtime path → **proceed** (no P0 block).
- The remaining divergences (substrate-vs-host gate code, cache≠run, host-exec LSP/gates,
  agent-no-proof-mode) are **known and assigned to Stages 4/5/1/2/3** — none is a hidden surprise.

**Conclusion:** Stage 0 reveals no blocking surprise on the shared-path axis. Stage 1 (LSP-liveness
certificate) is **safe to begin** after review.
