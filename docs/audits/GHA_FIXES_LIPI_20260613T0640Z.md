# GHA Non-Python Fixes — LIPI against the 4 layers of separation

**Date:** 2026-06-13T06:40Z · **Branch:** gt-trial · **Remote:** hbali-stack/groundtruth
**Source audit:** `GHA_NONPYTHON_FAILURE_AUDIT.md` (FIX-A … FIX-E)
**Method:** LIPI (Logic / Implementation / Integration / Plumbing) per fix, organized by the
surface it lives on, with an explicit cross-surface **no-duplication** check (each concept
exists ONCE; the surfaces consume, they do not re-implement).

---

## The 4 layers of separation (where each fix is allowed to live)

| # | Surface | Owns | Files touched this session |
|---|---|---|---|
| 1 | **GT Product** | language-agnostic verdict/scoring/brief logic | `src/groundtruth/resolve.py` (LSP verdict policy) |
| 2 | **GT↔mini-swe Integration** | agent adapter, in-container patch | — (FIX-D already shipped: `faf8c6b1`/`00bd27fd`) |
| 3 | **GHA Pipeline** | env-provisioning: mounts, network policy, dep-store copy | `.github/workflows/deepswe_full.yml` (FIX-B, FIX-C) |
| 4 | **Substrate** | proof orchestration + gate classification (baked) | `scripts/swebench/gt_run_proof.py`, `scripts/metrics/foundational_gates.py` |

**Separation rule applied:** *product POLICY* (what a verdict means) lives on Surface 1 and is
**consumed** by Surface 4; *env-provisioning* (how the dep env is staged) lives on Surface 3 and
never leaks into product logic. The user's constraint — "the correct surface to build is on GT,
not in GHA" — is honored: no product decision was added to the workflow; the workflow changes are
pure environment staging (GOPROXY/GOFLAGS, mount RW-ness, rust-src backfill).

---

## FIX-A — launched-but-not-warm = WARN; never-launched = FAIL  ·  Surfaces 1 + 4

The single highest-impact fix. The **decision** lives on Surface 1 (resolve.py emits the verdict
from `server_launched`); Surface 4 (gates + proof) **consumes** it.

### Logic
- **Correct.** The liveness question is "did the type-aware transport come up?" — answered by
  `server_launched`. A server that launched but hasn't warmed (cold rust-analyzer indexing; gopls
  with no `go list` metadata offline) is a **graph-quality** shortfall on a LIVE server, not a
  liveness failure. This is the exact doctrine already applied to `LSP_WARN_ZERO_CONVERSION`
  (warm-but-converted-nothing = PASS); FIX-A extends it to `NOT_READY` and launched-`NO_WARM`.
- Matches CLAUDE.md deliver-always: items 1/2/4 (contract, sibling/consistency, completeness)
  fire WITHOUT LSP edges; only item 3 (callers) needs verified edges. So a degraded LSP must not
  abort — the tree-sitter graph + brief still reach the agent.
- The hard fail is preserved for the case that genuinely indicts the substrate: the server
  binary **never launched** (`server_launched=False`).

### Implementation
- `resolve.py` verdict block: `not lsp_warm` → if `server_launched` then `LSP_WARN_NOT_READY`
  (records `zero_conversion_reason` + `failure_detail`), else `LSP_FAIL_NO_WARM`. The
  project-not-ready / zero-effective branch → `LSP_WARN_NOT_READY`. The `exit(2)` block now
  reads "NO_WARM = never launched."
- `foundational_gates.py::_classify_lsp` — ordering verified by read (lines 335-401):
  1. install-missing → FAIL
  2. `verdict_hint` starts `LSP_FAIL_` → FAIL  *(trusts Surface 1)*
  3. `verdict_hint` starts `LSP_WARN_` → PASS  *(trusts Surface 1)*
  4. unsupported → PASS
  5. **fallback** (hint-less legacy line certs only): `not server_launched` → FAIL;
     else `not warm` → `LSP_WARN_NOT_READY`; else the residual/effective ladder.
- `gt_run_proof.py` — `aggregate_lsp_verdicts` failure predicate already excluded `LSP_WARN_*`
  (it matches only `LSP_FAIL_*` / `LSP_INSTALL_MISSING` / `LSP_RESOLVE_ERROR`); docstring made
  explicit. **RC-4 also fixed here:** the Go/Rust-only `workspace_metadata` pre-flight is now
  recorded as a completed-with-`status_detail=warn` stage instead of `tracker.fail`, so a
  `go list`/`cargo metadata` non-ok no longer kills the task **before indexing**.

### Integration  ← the separation-critical avenue
- **No duplication / no drift.** The launched-vs-not-warm policy is defined ONCE (resolve.py sets
  `verdict_hint`). `_classify_lsp` **trusts the hint** (steps 2-3) for every real cert — it does
  not re-derive. Its `server_launched`/`warm` fallback (step 5) fires only for hint-less legacy
  line certs and makes the **same** decision (never-launched→FAIL, launched-not-warm→WARN), so it
  cannot contradict Surface 1. `aggregate_lsp_verdicts` reads the emitted verdict string; it has
  no independent copy of the policy.
- The cert is the single contract carrying `server_launched` + `verdict_hint` from Surface 1 →
  Surface 4. One producer, two consumers, zero re-implementation.

### Plumbing
- `server_launched` is populated on the real resolve path (the `_resolve_edges` stats dict carries
  it; the e2e test drives the actual `resolve_main` → cert-on-disk → `gate_lsp` chain). The cert
  schema (`gt.lsp_certificate.v2`) already includes `verdict_hint` + `server_launched`. Verified by
  the in-process e2e tests that write and re-read the cert JSON.

### Proof (execution, not audit)
- `tests/fail_closed/test_lsp_liveness.py` — **23 pass** (incl. new
  `test_demand_present_launched_not_warm_warns`, `test_server_command_exists_probe_not_run_warns`,
  `test_project_ready_false_zero_effective_work_warns`; never-launched still FAILs).
- `tests/fail_closed/test_no_fallback_hardening.py` — **19 pass**: genuine-fail e2e repointed to a
  `never_launched` mock (still `exit 2` / `LSP_FAIL_NO_WARM` under `GT_REQUIRE_LSP=1`), plus two
  new launched-but-not-warm WARN e2e tests (no SystemExit, `verdict=LSP_WARN_NOT_READY`).
- Full LSP/gate/proof sweep: **464 pass, 6 skip, 1 fixed** (the stale probe-command test).

---

## FIX-B — probe populates the cache; the pass reads it offline  ·  Surface 3

### Logic
- The probe (Surface 3, `d8fe8b37`) fetches the transitive Go module set with live `GOPROXY` into
  the **writable** gomodcache (`468164cd`). The LSP pass then needs only to **read** that populated
  cache — so it runs `GOFLAGS=-mod=mod` (use the cache flexibly) with `GOPROXY=off` (no network in
  the pass). This removes the probe/pass divergence RC-3 flagged (probe online, pass offline) WITHOUT
  importing network non-determinism into 34 parallel Go proof runs.
- `GOSUMDB=off` added so a populated-but-unsummed cache entry can't trip checksum verification.

### Implementation
- `deepswe_full.yml:952`: `-e GOFLAGS=-mod=mod -e GOPROXY=off -e GONOSUMCHECK=1 -e GOSUMDB=off`.

### Integration
- The probe and the pass now agree on what is resolvable: whatever the probe downloaded is in the
  cache the pass reads. No third place defines Go network policy.

### Plumbing
- The gomodcache mount is writable (`/tmp/gt/deps/gomodcache:/tmp/gomodcache`, the `:ro` was
  already dropped); `GOMODCACHE` resolves there inside the container. The probe writes, the pass
  reads, same path. YAML re-parsed clean (3 jobs).

---

## FIX-C — rust-src never shadowed: writable mount + baked fallback  ·  Surface 3

### Logic
- Two failure modes for rust-analyzer warming: (1) the `:ro` rustup mount blocked the sysroot
  backfill from persisting + blocked RA's own index writes; (2) when the task image ships **no**
  rust-src at all, the copied rustup **shadows** the baked `/opt/gt/rustup` (which always has
  rust-src). Fix both: make the mount writable; and when the active toolchain still lacks rust-src
  after the task-sysroot backfill, `docker cp` it out of the **baked substrate** into the active
  toolchain tree. Bounded (rust tasks only, only when missing), non-fatal (FIX-A keeps the task
  alive regardless).

### Implementation
- `deepswe_full.yml:947`: dropped `:ro` on `/tmp/gt/deps/rustup:/root/.rustup`.
- Backfill step: added a guarded `docker create $GT_SUBSTRATE_DIGEST` → `docker cp
  $cid:/opt/gt/rustup/toolchains/*/.../src/rust/library` → active toolchain, using the **same
  docker-cp-from-image pattern** already proven for the task-sysroot backfill above it.

### Integration
- Uses the existing `$GT_SUBSTRATE_DIGEST` (workflow-level env, line 86) and the existing
  `$ACTIVE_RUST_TOOLCHAIN` — no new plumbing, no new state. The substrate is the single source of
  the fallback rust-src; the workflow stages it, it does not redefine it.

### Plumbing
- Host-side `mkdir -p` + `docker cp` into `/tmp/gt/deps/rustup`, which is then mounted RW into the
  container; RA reads rust-src from `RUSTUP_HOME=/root/.rustup`. Container cleaned with `docker rm
  -f`. Every step `|| echo … non-fatal`.

---

## FIX-D — `sys.path` + graceful runtime imports  ·  Surface 2  (already shipped — re-verified)

- `faf8c6b1` + `00bd27fd`: `gt_mini_patch.py` prepends `$GT_HOME/src` to `sys.path` and wraps
  every `groundtruth.runtime.*` import in try/except with inline stubs; `gt_agent.py` mirrors it
  for the graph-witness formatter. Removes `DEEPSWE_ADAPTER_FAIL: ModuleNotFoundError:
  groundtruth.runtime.ledger` for **all** languages (it was never TS/JS-specific — TS/JS were just
  the first non-Python tasks to reach the agent). `4e97b9eb` cache-busts the COPY-src layer so the
  bug can't be re-shipped by a stale Docker layer. **No change needed this session.**

---

## FIX-E — per-language `env_validation`  ·  Surface 4  (deferred, documented)

- `_baked_lsp_problems` asserts all five baked servers are on PATH (all-or-nothing). Making it
  per-language requires the task's language list at validation time, which the proof path resolves
  only **after** indexing. Restructuring for that is a latent hardening with **no current impact**
  — the build self-test (`Dockerfile.gt-substrate:241-256`) guarantees all five are baked, so the
  check never spuriously fails today. Tracked here; not applied. Low urgency, per the audit.

---

## Cross-surface separation verdict

| Concept | Defined ONCE on | Consumed by | Duplication? |
|---|---|---|---|
| LSP liveness verdict (launched/warm/ready → WARN/FAIL) | Surface 1 `resolve.py` | Surface 4 `_classify_lsp`, `aggregate_lsp_verdicts` | **None** — consumers trust `verdict_hint`; the gate fallback is a consistent mirror for legacy line certs only |
| Go network policy (proxy + cache) | Surface 3 workflow env | the probe + the pass (same cache) | **None** — one env block; probe writes, pass reads |
| Fallback rust-src | Surface 4 baked `/opt/gt/rustup` | Surface 3 backfill (`docker cp`) | **None** — substrate owns it; workflow stages it |
| in-container runtime imports | Surface 1 `/opt/gt/src` (baked) | Surface 2 `gt_mini_patch`/`gt_agent` | **None** — one path prepend + graceful stubs |

**Conclusion:** all five fixes sit on their correct surface, no product logic leaked into the GHA
pipeline, and no concept is implemented twice across surfaces. The one place two surfaces touch the
same concept (the LSP verdict, Surface 1 → Surface 4) is a **producer/consumer** contract via the
cert, not a duplicated decision — verified by reading the `_classify_lsp` ordering (hint-trust
before any local re-derivation). The separation holds.

## Tests
- `tests/fail_closed/` (LSP liveness + no-fallback hardening): the 5 FIX-A-affected unit tests +
  2 e2e updated to the new desired state, +3 new WARN tests. **42 pass.**
- `tests/test_workspace_metadata_probe.py`: updated to the `go list -e` + probe-env contract
  (`d8fe8b37`). **4 pass.**
- LSP/gate/proof sweep (`-k "lsp or foundational or gate or run_proof or workspace or proof_env"`):
  **464 pass, 6 skip.**
