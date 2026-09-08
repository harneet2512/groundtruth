# GT Pipeline Architecture — Surface Boundaries

Date: 2026-06-12
Branch: `gt-trial`

## Why This Document Exists

The merge of the substrate-reproof branch (`7b76d6ad`) broke all Py/TS/JS tasks
with `ModuleNotFoundError: No module named 'groundtruth.runtime.ledger'`. Root
cause: `gt_mini_patch.py` imports from `groundtruth.runtime.*` (new modules added
by CP011-015), but the substrate's baked `src/groundtruth/` didn't include them
due to Docker layer caching serving a stale `COPY src` layer.

This exposed a fundamental problem: changes were being made on multiple surfaces
simultaneously (GT product code, substrate Dockerfile, GHA workflow, foundational
gates) without a clear contract between them. When surface A changed what it
exports and surface B changed what it expects in the same commit, the integration
broke at the boundary.

## The Three Surfaces

### Surface 1: GT Product (the source of truth)

**Location:** `artifact_deepswe/` + `src/groundtruth/`
**Runs:** Inside the agent container (base64-injected from checkout at runtime)
**What it does:** All intelligence — phase detection, obligation tracking, action
templates, context budgeting, evidence delivery, oracle gate, retry classification
**Tested:** Locally against frozen trajectories, no GHA needed

### Surface 2: Substrate (the baked environment)

**Location:** `docker/Dockerfile.gt-substrate` + what `COPY src` and `COPY scripts` bake
**Runs:** Pre-agent in a pinned, immutable container
**What it does:** Index the repo (gt-index), run LSP resolve pass, run foundational
gates, generate brief, emit certificates
**Why it exists (not "what it does"):**
- gt-index Go binary can't be built per-task (takes 2 min + needs CGO)
- ONNX embedder models (300MB) can't be downloaded per-task (HF rate limits)
- LSP servers (pyright/gopls/rust-analyzer/tsserver) can't be installed per-task
- The proof step validates substrate integrity before spending on the model

### Surface 3: GHA Workflow (the orchestrator)

**Location:** `.github/workflows/deepswe_full.yml`
**Runs:** On GHA runner (ubuntu-latest)
**What it does:** Pull task image, extract repo + deps, run substrate proof,
mount artifacts, run pier with the agent, collect results
**Why it exists:** Orchestration only. No GT logic should live here.

## The Contract Between Surfaces

```
Surface 3 (GHA)
  → pulls task image, extracts repo to /tmp/gt/src
  → runs Surface 2 (substrate) with repo mounted /work:ro
  → substrate emits: graph.db, brief.txt, certificates, gate report
  → GHA mounts artifacts for the agent

Surface 2 (substrate)
  → indexes repo with baked gt-index binary
  → runs LSP resolve with baked LSP servers
  → runs foundational gates with baked scripts/metrics/
  → generates brief with baked src/groundtruth/pretask/
  → emits artifacts to /gt_artifacts

Surface 1 (GT product)
  → base64-injected into agent container from checkout
  → reads substrate artifacts (graph.db, brief.txt, certs) READ-ONLY
  → observes agent actions, delivers context, tracks obligations
  → NEVER rebuilds the graph, NEVER re-runs LSP, NEVER regenerates brief
```

## What Goes WHERE and WHY

| Component | Surface | Why there, not elsewhere |
|---|---|---|
| gt-index (Go binary) | Substrate | CGO build takes 2 min, needs gcc — can't do per-task |
| ONNX embedder models | Substrate | 300MB download, HF rate limits — bake once |
| LSP servers (5 binaries) | Substrate | Install takes 1-2 min each — bake once |
| foundational_gates.py | Substrate | Pre-agent validation — must run BEFORE model spend |
| v1r_brief.py | Substrate | Brief generation needs graph.db — runs in substrate |
| resolve.py | Substrate | LSP pass needs baked servers — runs in substrate |
| gt_mini_patch.py | GT Product | Agent-side hooks — injected from checkout |
| gt_oracle.py | GT Product | Oracle logic — injected from checkout |
| gt_agent.py | GT Product (pier host) | Adapter — runs on GHA runner, imported from checkout |
| context_policy.py | GT Product | Phase policy — imported by gt_mini_patch |
| action_translation.py | GT Product | Templates — imported by gt_mini_patch |
| context_budget.py | GT Product | Budget trim — imported by gt_mini_patch |
| trajectory_state.py | GT Product | State tracking — imported by gt_mini_patch |
| obligations.py | GT Product | Obligation model — imported by gt_oracle |
| ledger.py | GT Product | Delivery ledger — imported by gt_mini_patch |

## The Rule That Was Violated

**gt_mini_patch.py imports from `groundtruth.runtime.*` — but these modules must
be available in the AGENT CONTAINER, not just the substrate container.**

The substrate bakes `src/groundtruth/` at `COPY src /opt/gt/src` time. The agent
container gets `gt_mini_patch.py` via base64 injection. When `gt_mini_patch.py`
does `from groundtruth.runtime.ledger import ...`, it needs `ledger.py` to be in
the agent container's Python path — which is `/opt/gt/src` (mounted from the
substrate artifacts).

If the substrate was built from a commit BEFORE `ledger.py` existed (or Docker
cached a stale layer), the import fails. This is what happened.

## How To Prevent This

1. **Surface 1 imports only from Surface 1.** `gt_mini_patch.py` should import
   from `artifact_deepswe/` siblings (gt_oracle.py, gt_oracle_sense.py) which are
   base64-injected together. If it needs `src/groundtruth/runtime/` modules, those
   modules must ALSO be base64-injected.

2. **OR: ship the runtime modules as part of the substrate.** The substrate already
   bakes `src/groundtruth/`. If `gt_mini_patch.py` imports from `groundtruth.runtime.*`,
   the substrate MUST be rebuilt from the SAME commit that adds those modules. And
   Docker layer caching must be invalidated on src/ changes.

3. **The current approach (option 2) works IF:** the substrate is always rebuilt
   from HEAD before dispatching. The failure was a stale cache, not a design flaw.
   But it means: never dispatch a run with a substrate older than the agent code.

## What The Substrate-Reproof Branch Did Right and Wrong

**Right:**
- Bumped Go to 1.24.4 (needed for ABS tasks)
- Fixed GOFLAGS from -mod=mod to -mod=readonly (broke go.work repos)
- Added `go list -e ./...` for platform-specific package handling
- Backfilled Rust sysroot from `rustc --print sysroot`
- Made LSP WARN verdicts fail-closed (correct once deps work)

**Wrong:**
- Changed `foundational_gates.py` and `resolve.py` in the SUBSTRATE surface
  simultaneously with the WORKFLOW surface changes
- Made LSP fail-closed without verifying Go/Rust deps actually resolve
- The changes were correct in isolation but untested as an integrated whole

## The Correct Integration Order

1. Fix Go/Rust deps in the WORKFLOW only (dep mounts, env vars)
2. Verify: Go/Rust LSP converts >0 edges on a proof sweep
3. THEN change the gates to fail-closed (foundational_gates + resolve)
4. THEN rebuild substrate with the gate changes
5. THEN run the benchmark

The substrate-reproof branch did steps 1+3 simultaneously. Steps 2 and 4 were
skipped. That's why it broke.

## Current State (post-revert)

- HEAD: `a2119a8b` (revert of the merge)
- All D1-D10 intelligence fixes intact
- LSP WARN soft-pass for Go/Rust (tasks proceed with tree-sitter-only graph)
- Py/TS/JS get full LSP conversions
- Prior run with substrate `2595578534` got 9/10 tasks passing (only arktype infra-fail)
- Rebuilding substrate from reverted HEAD now

## When To Re-Integrate Go/Rust LSP Fixes

After the benchmark. The fixes are correct but need:
1. A proof sweep verifying Go/Rust converts >0 edges with the dep mounts
2. Gate changes applied AFTER the conversion is verified
3. Substrate rebuild AFTER gate changes
4. Integration test BEFORE benchmark
