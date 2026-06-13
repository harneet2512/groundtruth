# Session Summary

## Date / Time
2026-06-13 — GHA non-Python failure fixes (FIX-A/B/C/D/E); LSP liveness verdict refined; LIPI vs the 4 surfaces

## Branch / Commit
`gt-trial` — local changes (to be committed + pushed to hbali-stack)

## Objective
Make Go/Rust/TS/JS reach the agent as reliably as Python on the DeepSWE GHA pipeline. Root-caused
in `GHA_NONPYTHON_FAILURE_AUDIT.md`: NOT Python-centric product logic, but env-provisioning
(gopls needs `go list`, RA needs `cargo metadata`+`rust-src`) colliding with a fail-closed
LSP-liveness gate. Apply FIX-A…E, document, LIPI against the 4 layers of separation.

## Implementation changes
1. **FIX-A (Surfaces 1+4, highest impact)** — LSP liveness axis is `server_launched`, not warm.
   Launched-but-not-warm (cold RA, gopls-no-metadata) → `LSP_WARN_NOT_READY` (**PASS**, deliver-
   always); only **never-launched** → `LSP_FAIL_NO_WARM` (exit 2). `resolve.py` verdict +
   `foundational_gates._classify_lsp` (consumes the hint) + `gt_run_proof.aggregate_lsp_verdicts`
   docstring + **`workspace_metadata` pre-flight made non-fatal (RC-4)**.
2. **FIX-B (Surface 3)** — Go probe populates the **writable** gomodcache (live GOPROXY,
   `d8fe8b37`); the LSP pass reads it offline with `GOFLAGS=-mod=mod` + `GOPROXY=off` + `GOSUMDB=off`
   (no proxy stampede, offline-deterministic, probe/pass agree).
3. **FIX-C (Surface 3)** — dropped `:ro` on the rustup mount + **baked-substrate rust-src fallback**
   (`docker cp` from `/opt/gt/rustup` when the task image ships none). Bounded, non-fatal.
4. **FIX-D (Surface 2)** — already shipped (`faf8c6b1`/`00bd27fd`): `sys.path` + graceful runtime
   imports. Re-verified, no change.
5. **FIX-E (Surface 4)** — per-language `env_validation` deferred (needs language list pre-index;
   no current impact; build self-test guarantees all 5 baked).

## Tests
- `tests/fail_closed/test_lsp_liveness.py` — **23 pass** (+1 WARN test; never-launched still FAILs).
- `tests/fail_closed/test_no_fallback_hardening.py` — **19 pass** (genuine-fail e2e repointed to a
  never-launched mock; +2 launched-not-warm WARN e2e).
- `tests/test_workspace_metadata_probe.py` — **4 pass** (updated to the `go list -e` + env contract).
- LSP/gate/proof sweep — **464 pass, 6 skip**.

## Result
All 5 fixes applied on their correct surface; no product logic leaked into GHA; no concept
duplicated across surfaces (LIPI verdict in `GHA_FIXES_LIPI_20260613T0640Z.md`). gt_gt.md §7 +
`GHA_NONPYTHON_FAILURE_AUDIT.md` updated.

## Regressions
None. The one stale test (`test_workspace_metadata_probe`, stale from `d8fe8b37`) fixed.

## Open blockers
- Substrate rebuild + digest pin required (FIX-A/B/C touch baked `resolve.py`/`foundational_gates.py`/
  `gt_run_proof.py` + the workflow); then re-proof Go/Rust to confirm they reach the agent.

## Next allowed action
1. Commit + push to hbali-stack → rebuild substrate → re-proof a Go + Rust task → confirm WARN-not-fail
   lets them reach pier → tenpack GT-on vs frozen baseline.

---

# Session Summary (prior)

## Date / Time
2026-06-12 — LSP proof boundary fixes (P0-04/05/02/06/07/11); Stage 1 code complete; tenpack not run

## Branch / Commit
`gt-trial` — local uncommitted changes (not pushed this session)

## Objective
Fix substrate proof failures from run `27387470440` (Go empty gomodcache, Rust LSP warm fail); document + LIPI each bug; finalize register; **do not** dispatch tenpack.

## What shipped

1. **P0-04** — Dynamic `GOMODCACHE` discovery + `dep_store_manifest.py` fail-closed for Go.
2. **P0-05** — Rust dep discovery, `rust-src`, RA `2026-06-08`, `gcc` in substrate Dockerfile.
3. **P0-02** — `proof_progress.json` + `proof_failure.json` substage tracking in `gt_run_proof.py`.
4. **P0-06/07** — `task_truth.json` authority; `reconciled_substrate_verdict.json`; metrics prefer task_truth.
5. **P0-11** — `artifact_deepswe/phase_policy.py` extracted; injected via `gt_agent.py`.
6. **Docs** — Bug register, `gt_gt.md` §17.10, `dispatch_tenpack_gt_on.sh` (script only).

## Tests
**17/17 passed** (dep_store_manifest, proof_progress, phase_policy, task_truth, gt_deep_metrics).

## Live runs
**None dispatched** (per user instruction).

## Open blockers
- Substrate image rebuild + digest pin required for P0-05 to take effect live.
- Go/Rust proof matrix must go green before Stage 2 tenpack.

## Next allowed action
1. Commit/push → rebuild substrate → re-proof 3 tasks → tenpack GT-on vs frozen baseline.

## Docs
- `.claude/reports/runs/validation_27387470440/GT_LSP_PROOF_HANDOFF.md`
- `.claude/reports/runs/validation_27387470440/ATOMIC_PRODUCT_BUG_REGISTER_20260612.md`
- `LATEST_TASK.md`

---

# Session Summary (previous)

## Date / Time
2026-06-12 — CP011–015 trajectory controller shipped + tenpack dispatch blocked on substrate proof

## Branch / Commit
`gt-trial` @ `df4c37c5` (pushed to `hbali-stack/groundtruth`).

## Objective
Ship §17.8 controller stack; validate on 10-task bugfree tenpack.

## What shipped
Commit `df4c37c5`: CP011–015, P6 verifier retry, P7 trajectory scorecard. Plan-scoped pytest **84/84** before push.

## Live runs
- Language smoke `27385688504`: pass on pinned digest.
- Tenpack `27386082651`: **failed** at substrate proof (`gt-run-proof rc=2`).

## Next allowed action
Triage substrate proof → fix (this session) → re-dispatch tenpack.
