# GHA Non-Python Failure Audit — Why Go/Rust/TS/JS die before the agent

**Date:** 2026-06-13T05:03Z
**Branch:** gt-trial · **Remote:** hbali-stack/groundtruth
**Scope:** the DeepSWE benchmark GHA pipeline (`deepswe_full.yml` + `deepswe_proof_sweep.yml`),
the pinned substrate (`docker/Dockerfile.gt-substrate`), the proof orchestrator
(`scripts/swebench/gt_run_proof.py`), the LSP dispatch + verdict engine
(`src/groundtruth/resolve.py`, `src/groundtruth/lsp/config.py`), the gate classifier
(`scripts/metrics/foundational_gates.py`), the dep-store validator
(`scripts/swebench/dep_store_manifest.py`), and the agent adapter
(`artifact_deepswe/gt_agent.py`, `artifact_deepswe/gt_mini_patch.py`).
**Task mix (manifest):** Go 34 · Python 34 · TypeScript 35 · Rust 5 · JavaScript 5 = 113.

---

## Bottom line up front

The pipeline is **not** Python-centric in its *product logic* — the indexer, gates, LSP
dispatch, brief, and embedder are all language-agnostic and config-driven. The asymmetry is
almost entirely **environment provisioning + gate policy** colliding with a hard LSP-liveness
requirement (`GT_REQUIRE_LSP=1`):

1. **Python/TS/JS resolve from source text alone** (pyright, tsserver are
   single-file-capable). They reach a real `textDocument/definition` answer with nothing but
   the mounted repo, so their LSP verdict is `LSP_ACTIVE_VALID` / `LSP_WARN_ZERO_CONVERSION`
   (a PASS) almost unconditionally.
2. **Go/Rust cannot.** gopls needs `go list` package metadata (a writable module cache + a
   reachable proxy or a complete vendored cache); rust-analyzer needs `cargo metadata` +
   `rust-src`. The pipeline tries to copy those stores out of each task image, but the stores
   are frequently **incomplete or absent**, and when they are, GT chose to **gate the entire
   run on a Go/Rust-only pre-flight** (`probe_workspace_metadata`) and on a
   **fail-closed LSP-liveness aggregation** that treats a launched-but-unproductive Go/Rust
   server as a hard failure. Python never hits either gate.
3. **TS/JS pass the proof but historically died at the agent step** on a *Python import bug*
   in the in-container patch (`groundtruth.runtime.ledger` not on `sys.path`), which is
   language-independent but surfaced as `DEEPSWE_ADAPTER_FAIL`. That is now fixed.

So the disadvantage is: **(a) two extra fail-closed gates that ONLY apply to Go/Rust**, gated
on **(b) dep stores that the eval images don't reliably ship**, plus **(c) a now-fixed
host-split import bug** that hit every language but was first observed on TS/JS.

---

## Part 1 — Per-language proof-path table

The proof path (`gt_run_proof.py::main`) runs these stages in order, per task:
`env_validation → dep_store → source_copy → workspace_metadata → index → lsp_pass → graph_cert
→ gates → brief_emit → artifact_contract`. The agent step then runs, then the **adapter
witness** step (`gt_prebuilt_active=true` + hash match).

| Stage | What it needs | Python | TypeScript | JavaScript | Go | Rust |
|---|---|---|---|---|---|---|
| **env_validation** (`gt_run_proof.py:296-317`) | baked binary for **every** server in `LSP_SERVERS` on PATH (pyright, tsserver, gopls, rust-analyzer, jdtls) + baked embedder + `gt-index`. **All-or-nothing**: a single missing server fails ALL languages. | ✅ baked | ✅ baked | ✅ baked | ✅ baked | ✅ baked |
| **dep_store** (`gt_run_proof.py:786-811`; `dep_store_manifest.py:128-143`) | validator only fails for **rust** (needs non-empty cargo + rustup). Go/Py/TS/JS: no requirement. | ✅ n/a | ✅ n/a | ✅ n/a | ✅ n/a | ❌ **DEP_STORE_EMPTY** if cargo/rustup not copied |
| **source_copy** | copytree of `/work:ro` → `/tmp/gt_work_src` | ✅ | ✅ | ✅ | ✅ | ✅ |
| **workspace_metadata** (`gt_run_proof.py:659-738`, called 864-888) | **Go/Rust ONLY.** Go: `go list -e ./...` must rc=0 (now with `GOFLAGS=-mod=mod` + live `proxy.golang.org`). Rust: `cargo metadata` must rc=0. **Skipped for Py/TS/JS.** | ⏭️ skip | ⏭️ skip | ⏭️ skip | ❌ **GO_WORKSPACE_METADATA_FAIL** if no proxy/cache/go.mod | ❌ **RUST_WORKSPACE_METADATA_FAIL** if cargo can't read |
| **index** (`gt-index`, tree-sitter) | source text only — fully language-agnostic | ✅ | ✅ | ✅ | ✅ | ✅ |
| **lsp_pass** (`gt_run_proof.py:894-996`; verdicts `resolve.py:1432-1486`) | a **warm** server (`LSP_FAIL_NO_WARM` if it never answers `workspace/symbol`). Then product-readiness: `LSP_FAIL_NOT_READY` if `project_ready=false` and 0 conversions. Aggregation fails closed on any `LSP_FAIL_*`. | ✅ pyright warms + answers from source | ✅ tsserver warms (readiness barrier handles lazy load) | ✅ tsserver warms | ⚠️ gopls warms but `project_ready=false` w/o `go list` → **LSP_FAIL_NOT_READY** | ⚠️ rust-analyzer **never warms in budget** (45s) while indexing → **LSP_FAIL_NO_WARM** |
| **graph_cert / gates** (`foundational_gates.py`) | Gate1 resolution (det≥name_match), Gate2 LSP (cert verdict), Gate3 embedder. In proof, `GT_GATES_DELIVER_ALWAYS=0` → any OFF gate fails. | ✅ | ✅ | ✅ | ⚠️ already dead at lsp_pass | ⚠️ already dead at lsp_pass/dep_store |
| **brief_emit / artifact_contract** | non-empty brief + all 8 artifacts | ✅ | ✅ | ✅ | (unreached) | (unreached) |
| **agent step** (`deepswe_full.yml:1013-1125`) | pier launches `GTMiniSweAgent`; in-container `gt_mini_patch.py` imports `groundtruth.runtime.*` | ✅ | ❌→✅ **was** ModuleNotFoundError: `groundtruth.runtime.ledger` (host-split, NOT language) — now `sys.path`+graceful | same as TS | (unreached) | (unreached) |
| **adapter witness** (`deepswe_full.yml:1127-1152`) | `[GT_META] gt_prebuilt_active=true` + `hook_graph_hash_matches_post_lsp=True` | ✅ | ✅ (after import fix) | ✅ | (unreached) | (unreached) |

Legend: ✅ pass · ⏭️ correctly skipped · ⚠️ degraded→fail-closed · ❌ hard fail · ❌→✅ fixed.

---

## Part 2 — Root causes, ranked and classified

### RC-1 — Go/Rust LSP needs build-env metadata; Python/TS/JS do not. **[ENV-PROVISIONING + language reality, NOT GT design]** — DOMINANT

This is the physics of the problem and it is correctly understood in the code comments
(`Dockerfile.gt-substrate:66-68, 80-81, 88-90`; `resolve.py:1455-1459`).

- **pyright / tsserver resolve `textDocument/definition` from source text alone.** They build
  their own module graph by walking imports in the mounted tree; no external package manager
  invocation is required to answer a definition query. So Python/TS/JS warm and answer with
  nothing but `/work:ro`.
- **gopls shells out to `go list` / `go env`** to load packages
  (`Dockerfile.gt-substrate:207-209`). Without a readable module graph it reports
  `project_ready=false` and every `definition` returns empty → `resolve.py:1447-1452`
  stamps `LSP_FAIL_NOT_READY`.
- **rust-analyzer needs `cargo metadata` + `rust-src`** for workspace discovery and stdlib
  types (`Dockerfile.gt-substrate:88-90`). Cold, it spends the whole readiness budget
  indexing and **never warms** → `resolve.py:710-715, 1443-1444` stamps `LSP_FAIL_NO_WARM`
  (the cert's `failed_empty=6620/6620, project_ready=false` shape noted at `resolve.py:874-888`).

**Confirmed by reading the code**, not hypothesized. The hypothesis in the task prompt is
correct.

### RC-2 — `GT_REQUIRE_LSP=1` + fail-closed verdict aggregation make a Go/Rust dep-env limitation kill the whole task. **[GATE-POLICY]** — the actual "dies before the agent" mechanism

The dep-env limitation (RC-1) is a *graph-quality* shortfall, not a substrate defect. But the
proof path converts it into a **hard task failure** in two places:

1. **`resolve.py:1473-1486`** — `LSP_FAIL_NO_WARM` under `GT_REQUIRE_LSP=1` does
   `sys.exit(2)`. This is the **rust-analyzer-cold** path: the server is baked and correct, it
   just hasn't finished indexing inside the budget → the language exits non-zero.
2. **`gt_run_proof.py:598-624` (`aggregate_lsp_verdicts`)** — flags as a failure any verdict
   in `{LSP_INSTALL_MISSING, LSP_FAIL_NO_WARM}` **or** `startswith("LSP_FAIL_")`. So
   `LSP_FAIL_NOT_READY` (the **gopls-no-metadata** path, `resolve.py:1447`) is caught even
   though resolve.py itself returns rc=0 for it. Under `GT_REQUIRE_LSP=1`, **one** failing
   known language → `LSP_LIVENESS_FAIL` → `tracker.fail("lsp_pass", ...)` → proof rc≠0 →
   `GT_RUN_PROOF_FAIL` (`deepswe_full.yml:961-981`) → the task dies **before pier ever runs**.

**Why Python sails through:** pyright reaches `LSP_ACTIVE_VALID` or, on a repo with no
in-scope method-call residual, `LSP_NO_OP_VALID_WITH_WARM_SERVER` — both PASS. It never
produces an `LSP_FAIL_*` verdict because it always warms and always answers.

**The asymmetry of the policy:** `LSP_WARN_ZERO_CONVERSION` (`resolve.py:1453-1461`) — a warm
server that converted nothing because the dep env is incomplete — is explicitly a **PASS**
(`foundational_gates.py:383-386`, `_classify_lsp` returns `ok=True`). That is the correct,
CLAUDE.md "correct-or-quiet" treatment. But Go/Rust frequently don't even reach
`ZERO_CONVERSION`; they land on `LSP_FAIL_NOT_READY` (gopls, `project_ready=false`) or
`LSP_FAIL_NO_WARM` (rust, never warmed), which are **fail-closed**. The line between
"warm-but-zero" (pass) and "not-ready/not-warm" (fail) is exactly the line that separates a
complete dep env from an incomplete one — i.e. the gate punishes the dep-provisioning gap
twice.

### RC-3 — The dep-store extraction assumes the task image ships a usable module cache / cargo home / rust-src. It often doesn't. **[ENV-PROVISIONING]**

`deepswe_full.yml:807-865` copies the stores out of the task image with a candidate-path
sweep. Strengths: it discovers `go env GOMODCACHE`, `CARGO_HOME`, `RUSTUP_HOME` dynamically
and backfills `rust-src` from the sysroot (`:846-857`). Weaknesses that cause Go/Rust death:

- **Go:** the copied module cache is only the deps the task image happened to pre-download.
  `go list ./...` needs the **transitive** set; the probe works around this by overriding
  `GOPROXY=https://proxy.golang.org,direct` + `GOFLAGS=-mod=mod` **for the probe only**
  (`gt_run_proof.py:679-680`). But the actual LSP pass runs with `GOPROXY=off`
  (`deepswe_full.yml:952`, Dockerfile `GOPROXY=off`), so even if the probe passes by
  downloading, gopls inside the substrate **cannot** fetch the same deps and can still report
  `project_ready=false`. The probe and the real pass have **divergent network policy** — the
  probe is more permissive than the thing it gates.
- **Rust:** the validator (`dep_store_manifest.py:134-143`) hard-requires non-empty `cargo`
  AND `rustup`. If the image's cargo home wasn't copied (non-default path missed by the
  candidate sweep, or the image simply doesn't have one because the build used a system
  toolchain), it's `DEP_STORE_EMPTY` before indexing. `rust-src` is *not* required by the
  validator (good — `468164cd` made it non-required), but rust-analyzer still needs it to
  warm, so a missing rust-src degrades to `LSP_FAIL_NO_WARM` (RC-2) anyway.
- The substrate bakes its **own** cargo+rustc+rust-src and Go toolchain as a fallback
  (`Dockerfile.gt-substrate:96-102, 207-215`), but the mounts are **read-only**
  (`deepswe_full.yml:946-947`: `/root/.cargo:ro`, `/root/.rustup:ro`) and `GOMODCACHE` points
  at the copied (possibly-incomplete) `/tmp/gomodcache`. A read-only incomplete cache cannot
  be completed at run time.

### RC-4 — The `workspace_metadata` pre-flight is a **Go/Rust-only extra gate** with no Python equivalent. **[GATE-POLICY]**

`probe_workspace_metadata` (`gt_run_proof.py:659-738`) returns `applicable=False` for anything
except go/rust (`:665-672`). For go/rust it runs `go list -e` / `cargo metadata` and, on rc≠0,
calls `tracker.fail("workspace_metadata", GO_WORKSPACE_METADATA_FAIL / RUST_WORKSPACE_METADATA_FAIL)`
(`:864-876`) — a hard stop **before indexing**. Python/TS/JS skip this entirely. This is a
deliberate "product truth for Go/Rust readiness" gate (`:662-663`), but it means Go/Rust have
**one more way to die that Python structurally cannot hit.** It is defensible (it front-loads a
failure that would otherwise surface as an opaque LSP no-op), but it is asymmetric by
construction.

### RC-5 — `DEEPSWE_ADAPTER_FAIL` on TS/JS was a host-split Python-import bug, language-independent. **[PLUMBING, now fixed]**

The in-container patch `gt_mini_patch.py:47-102` imports `groundtruth.runtime.{ledger,
action_translation, context_budget, trajectory_state, verification_horizon}`. These live in the
substrate's baked `/opt/gt/src`, but the in-container **agent process** (launched by pier via
`docker compose exec`) did not have `/opt/gt/src` on `sys.path` and the package was not pip-
installed in the task image → `ModuleNotFoundError: groundtruth.runtime.ledger`. The adapter's
`_emit_graph_witness` import block (`gt_agent.py:709-714`) raises `DeepSweAdapterError` on any
import failure under proof/substrate mode → `DEEPSWE_ADAPTER_FAIL`.

**Why it looked TS/JS-specific:** it is not. It fires on whatever language's task *reached the
agent step first*. Python tasks that reached the agent would hit the identical bug. It surfaced
on TS/JS because Go/Rust were dying earlier (RC-1/2/3) and never got to the agent, so TS/JS were
the first non-Python tasks to actually run pier.

**Fix (committed):** `faf8c6b1` ("add /opt/gt/src to sys.path before groundtruth.runtime
imports") + `00bd27fd` ("graceful fallback for all groundtruth.runtime imports"). Now
`gt_mini_patch.py:52-102` prepends `$GT_HOME/src` to `sys.path` and wraps every import in
`try/except ImportError` with inline stubs (`_RUNTIME_AVAILABLE=False` path). Same pattern in
`gt_agent.py:723-732` for the `graph_certificate` formatter.

### RC-6 — `env_validation` baked-server check is **all-or-nothing across all languages**. **[GATE-POLICY, latent]**

`_baked_lsp_problems` (`gt_run_proof.py:324-348`) asserts **every** command in `LSP_SERVERS`
(pyright, tsserver, gopls, rust-analyzer, jdtls) is on PATH, and `validate_proof_env` aborts the
**whole proof** (`SUBSTRATE_NOT_PORTABLE`) if any is missing. This is currently fine (the
Dockerfile bakes all five and self-tests them at build, `Dockerfile.gt-substrate:241-256`), but
it means a future substrate that drops/breaks ONE server (e.g. a bad rust-analyzer release URL)
fails **Python tasks too**. It is a shared single point of failure, not a per-language one.

### RC-7 — No Python-centric *product* logic found. **[refutes part of the hypothesis]**

I specifically looked for hardcoded `.py` assumptions, python-only fallbacks, and test-runner
detection that would silently degrade other languages, and **did not find them in the proof
path**:

- `LSP_SERVERS` / `LANGUAGE_IDS` / `DIAGNOSTIC_CODES` (`lsp/config.py`) are full multi-language
  tables; `_LANG_TO_EXT` and `_KNOWN_SERVERS` are **derived** from them
  (`resolve.py:66-99`), so they can never advertise a language the config can't serve.
- `_detect_langs` (`gt_run_proof.py:435-446`) resolves **every** language present, dominant
  last, and the per-language certs aggregate without overwrite (`:937-996`).
- The hover/return-type parser in `resolve.py:991-1024` has explicit Go (`func (r *T) M() Ret`),
  Rust (`fn f() -> T`), and TS/JS (`function f(): Ret`) branches — not Python-only.
- Gate1/2/3 thresholds are relative/per-task (`foundational_gates.py:71-105`), not keyed to
  Python.
- **The one Python-only branch** is benign: `resolve.py:625-646` writes a minimal
  `pyrightconfig.json` so pyright doesn't refuse `str | None`. That *helps* Python; it does not
  harm others.

The single residual Python-ism worth flagging: `_detect_lang`/`_demand_scope_files` default to
`"python"` on a read error (`gt_run_proof.py:392, 394`), and `gate_embedder`'s probe strings are
English/Python-flavored — neither degrades a non-Python run because the real language list comes
from the graph, and the embedder probe is language-agnostic cosine.

---

## Part 3 — Minimal fixes to make Go/Rust/TS/JS reach the agent as reliably as Python

Ordered by impact. The goal is **stop non-Python tasks dying before pier**, without
benchmaxxing and without harming the model.

### FIX-A (highest impact) — Treat Go/Rust "warm-transport, dep-env-incomplete" as a PASS, exactly like `LSP_WARN_ZERO_CONVERSION`. **[gate-policy]**

The dep-env limitation is a *graph-quality* axis, not a liveness failure. CLAUDE.md's own
deliver-always doctrine (`foundational_gates.py:925-952`) already says graph-quality gates must
not abort the agent. Apply the same logic to the LSP-liveness aggregation:

- In `resolve.py`, **demote `LSP_FAIL_NOT_READY` to a WARN** (`LSP_WARN_NOT_READY`) when the
  transport is warm (`server_launched && warm_probe_ok`) but `project_ready=false` — i.e. the
  server is alive, the workspace just isn't loadable offline. Keep `LSP_FAIL_NO_WARM` as a hard
  fail **only** when the server never launched/answered at all (a genuine substrate break).
- Correspondingly, make `aggregate_lsp_verdicts` (`gt_run_proof.py:613-617`) treat
  `LSP_WARN_*` as non-failures (it already implicitly does — they don't match the failure
  predicates) and **not** blanket-match `startswith("LSP_FAIL_")` for the not-ready case.
- For **rust-analyzer cold-never-warms**: this is the harder one because it's `NO_WARM`, not
  `NOT_READY`. Two sub-options:
  - **A1 (preferred):** raise the rust readiness budget from 45s and/or add a one-time
    "warm wait" that polls `workspace/symbol` until RA answers or a hard ceiling (e.g. 120s),
    so a server that *will* warm gets the chance. Bounded, repo-size-agnostic.
  - **A2:** if RA still hasn't warmed at the ceiling, classify `LSP_WARN_RA_INDEXING` (warm-
    transport pending) rather than `LSP_FAIL_NO_WARM`, on the same "transport not the product"
    logic — provided the binary did launch.

**Net effect:** Go/Rust reach the agent with a structurally-complete tree-sitter graph + a
brief, exactly as the "deliver-always" philosophy intends, instead of dying at `lsp_pass`. This
is the single change that closes most of the gap. **Generalized** (no per-task/per-repo logic),
**research-backed** (the graph still carries CHA/RTA structural edges + contracts/siblings/
completeness that don't need LSP — CLAUDE.md "items 1,2,4 always fire"), **correct-or-quiet**
(the cert still records the real zero-conversion + WHY).

### FIX-B — Make the Go workspace probe and the gopls LSP pass use the **same** network policy. **[env-provisioning]**

Right now the probe uses live `GOPROXY` (`gt_run_proof.py:680`) but the LSP pass uses
`GOPROXY=off` (`deepswe_full.yml:952`). Either:
- **B1:** give gopls the same live proxy + a **writable** module cache during the pass (drop
  the `:ro` on the gomodcache mount — `468164cd` already moved toward "writable gomodcache"),
  so gopls can fetch the transitive deps the probe proved are fetchable; **or**
- **B2:** make the probe honor `GOPROXY=off` too, so it fails-closed honestly when the offline
  cache is incomplete instead of passing on a download the real pass can't repeat. (B1 is
  better for actually reaching warm; B2 just makes the gate consistent.)

Without this, a Go task can pass `workspace_metadata` and *still* fail `lsp_pass` for the same
underlying reason — the worst outcome (spends the probe download, still dies).

### FIX-C — Harden the Rust dep-store copy + make the substrate's baked toolchain usable. **[env-provisioning]**

- Mount the substrate's **own** baked cargo/rustup writable (or layer the task's copied store
  over the baked one) so rust-src is always present even when the task image lacks it. The
  substrate already bakes `rust-src` (`Dockerfile.gt-substrate:100`); the `:ro` mount of the
  task's (possibly rust-src-less) rustup at `/root/.rustup` **shadows** the baked one. Use the
  baked `RUSTUP_HOME=/opt/gt/rustup` as the fallback when the copied store lacks rust-src,
  rather than overmounting it.
- The `dep_store_manifest.py:134-143` rust requirement on non-empty `cargo` is reasonable, but
  consider downgrading `DEP_STORE_EMPTY` to a WARN that lets the **baked** cargo serve, so a
  task image without a cargo home still runs against the substrate toolchain.

### FIX-D (already done — verify) — `sys.path` + graceful imports for the in-container patch. **[plumbing]**

Confirmed present (`gt_mini_patch.py:52-102`, `gt_agent.py:723-732`). This removes
`DEEPSWE_ADAPTER_FAIL: ModuleNotFoundError: groundtruth.runtime.ledger` for **all** languages.
Keep the belt-and-braces `--ae`-forwarded env + the `/opt/gt/src` prepend; do not regress the
COPY-src cache-bust (`4e97b9eb`, `Dockerfile.gt-substrate:184-191`) that caused the stale-layer
re-introduction of this exact bug.

### FIX-E (latent hardening) — Make `env_validation`'s baked-server check **per-language**, not all-or-nothing. **[gate-policy, low urgency]**

`validate_proof_env` should fail a task only on the absence of the server(s) for the
language(s) **actually present in that task's graph**, not all five. This stops a future
single-server breakage (e.g. a dead rust-analyzer release URL) from failing Python/TS tasks.
Low urgency because the build self-test currently guarantees all five.

---

## Part 4 — What's already committed vs still needed

### Already committed (this session's git log)

| Commit | Fix | Addresses |
|---|---|---|
| `cb3a6530` | Go 1.24.4 + `GOFLAGS=-mod=readonly` + non-fatal Rust dep manifest | RC-1 (Go toolchain), partial RC-3 |
| `31aad5b6` | bake Rust toolchain (cargo+rustc+rust-src) + sysroot backfill | RC-1/RC-3 (Rust) — `Dockerfile.gt-substrate:96-102`, `deepswe_full.yml:846-857` |
| `468164cd` | writable gomodcache + rust_src **non-required** in validator | RC-3 — `dep_store_manifest.py` rust-src dropped from hard-require |
| `d8fe8b37` | `go list -e` with `-mod=mod` + live GOPROXY for the workspace probe | RC-1 (Go probe) — `gt_run_proof.py:679-680` |
| `faf8c6b1` | add `/opt/gt/src` to `sys.path` before `groundtruth.runtime` imports | **RC-5 (the TS/JS `DEEPSWE_ADAPTER_FAIL`)** — `gt_mini_patch.py:52-54` |
| `00bd27fd` | graceful fallback for all `groundtruth.runtime` imports | RC-5 — `gt_mini_patch.py:56-102` |
| `4e97b9eb` | cache-bust COPY src layer with `GT_COMMIT_SHA` | RC-5 root cause (stale Docker layer re-shipping the bug) — `Dockerfile.gt-substrate:184-191` |
| (in `resolve.py`) | `_note_failure_detail` stamps WHY gopls/RA produced 0 conversions | diagnosability of RC-1/RC-2 — `resolve.py:467-485, 874-888` |
| (in `resolve.py`) | `LSP_WARN_ZERO_CONVERSION` is a PASS for warm-but-dep-limited | partial RC-2 — `resolve.py:1453-1461`, `foundational_gates.py:383-386` |

**Net of what's committed:** TS/JS should now pass the adapter witness (RC-5 closed). Go has a
toolchain + a permissive probe (RC-1 partially closed). Rust has a baked toolchain + a non-
required rust-src (RC-3 partially closed).

### APPLIED this session (2026-06-13) — the "dies before the agent" gap closed

1. **FIX-A — APPLIED.** Go `LSP_FAIL_NOT_READY` and cold-RA `LSP_FAIL_NO_WARM` (when the server
   **launched**) are demoted to `LSP_WARN_NOT_READY` (a PASS). The hard fail is reserved for a
   server that **never launched** (`server_launched=False` = a genuine substrate break). Three
   substrate files + the `workspace_metadata` pre-flight + 2 test files:
   - `resolve.py` — verdict block: launched-but-not-warm → `LSP_WARN_NOT_READY`; never-launched
     → `LSP_FAIL_NO_WARM`; demoted the project-not-ready path to WARN.
   - `foundational_gates.py::_classify_lsp` — `LSP_WARN_*` hints pass; not-warm-but-launched →
     WARN/ok; project-not-ready+zero-effective → WARN/ok.
   - `gt_run_proof.py` — `aggregate_lsp_verdicts` docstring (the failure predicate already
     excluded `LSP_WARN_*`); **`workspace_metadata` pre-flight made non-fatal** (RC-4): a
     `go list`/`cargo metadata` non-ok is recorded as a completed-with-warn stage, not a
     `tracker.fail`, so Go/Rust no longer die before indexing.
   - `tests/fail_closed/test_lsp_liveness.py` (+1 WARN test) and
     `tests/fail_closed/test_no_fallback_hardening.py` (+2 WARN tests; the genuine-fail e2e
     repointed to a never-launched mock) updated to the new desired state. **42 fail-closed
     tests green.** **(GATE-POLICY)**
2. **FIX-B — APPLIED.** The probe populates the **writable** gomodcache via its live-`GOPROXY`
   fetch (`d8fe8b37` + `468164cd`); the LSP pass now reads that populated cache with
   `GOFLAGS=-mod=mod` while staying `GOPROXY=off` — **offline-deterministic** (no 34-task proxy
   stampede), and the probe/pass no longer diverge on what's resolvable. `GOSUMDB=off` added.
   `deepswe_full.yml:952`. **(ENV-PROVISIONING)**
3. **FIX-C — APPLIED.** (a) Dropped the `:ro` on the rustup mount (`deepswe_full.yml:947`) so
   the sysroot backfill persists and rust-analyzer can write its index. (b) Added a
   **baked-substrate rust-src fallback**: when the task image lacks rust-src entirely (copied
   rustup AND its sysroot), `docker cp` rust-src out of the baked `/opt/gt/rustup` (which always
   ships it) into the active toolchain tree — bounded, non-fatal, FIX-A keeps the task alive if
   it fails. `deepswe_full.yml` backfill step. **(ENV-PROVISIONING)**
4. **FIX-E — DEFERRED (documented).** Per-language `env_validation` needs the task's language
   list at validation time, which the proof path resolves only after indexing; restructuring for
   it is a latent hardening with no current impact (the build self-test guarantees all five
   servers are baked). Tracked, not applied. **(GATE-POLICY, low urgency)**

Plus the stale-from-`d8fe8b37` unit test (`tests/test_workspace_metadata_probe.py`) updated to
assert the current `go list -e` command + probe-only env override.

### Classification summary

- **Python-centric *design*: essentially none** (RC-7). The product logic is language-agnostic
  and config-driven. The one Python-only branch (pyrightconfig) only helps Python.
- **Env-provisioning (the real root): RC-1, RC-3, FIX-B, FIX-C.** Go/Rust LSPs need build-env
  metadata the eval images don't reliably ship, and the substrate's read-only fallback mounts
  can't complete an incomplete store.
- **Gate-policy (the amplifier that turns a quality shortfall into task death): RC-2, RC-4,
  FIX-A, FIX-E.** `GT_REQUIRE_LSP=1` + fail-closed verdict aggregation + a Go/Rust-only
  pre-flight gate punish the env gap with a hard stop, while Python structurally cannot reach
  those failure verdicts.

**The one change that most closes the gap is FIX-A** — apply the existing "deliver-always /
warm-but-zero is a PASS" doctrine to the not-ready / cold-warm Go/Rust verdicts, so a
structurally-complete graph + brief reaches the agent even when the language's type-aware server
can't fully resolve offline. That is consistent with CLAUDE.md (items 1,2,4 always fire; LSP
callers are the only edge-dependent pillar) and is generalized, not benchmaxxing.
