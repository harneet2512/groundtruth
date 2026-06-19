# Session Handoff — 2026-06-12

Branch: `gt-trial`
Local HEAD: `d8fe8b37`
Substrate building: run `27444631131` (Go 1.24.4 + Rust toolchain + Go probe fix)

## What was done this session

### D1-D10 defect closure (CP011-015 LIPI)
Fable LIPI found 10 defects in the CP011-015 single Cursor commit. All fixed:

| ID | Fix | Commit |
|---|---|---|
| D1 | Budget dedup post-gate (commit_delivered scoped to l3b.evidence winner) | `77dc857c` + `7da50622` |
| D2 | Dead host-side reset removed (state fresh by construction) | `7da50622` |
| D3 | Pre-submit boost uses composite(_SEV_GATE+1) — beats horizon; nonedit_streak dropped at >90% | `77dc857c` |
| D4 | ObligationTracker monotonic ratchet + min symbol 4 | `8d505603` |
| D5 | Templates: caller_risk for caller direction, w["target"] binding, append don't replace | `8d505603` + `d7979a4e` |
| D6 | Env regex split: agent-caused breakage (ImportError etc) now gets retry feedback | `e7fd256e` |
| D7 | Ledger defers judgment one turn, decay on consumed only, threshold 3 | `8d505603` + `7da50622` |
| D8 | Phase thresholds data-derived (ORIENT=3% of step_limit) | `0caa5878` |
| D9 | Closed — Phase.VIEW covers searching state | `0caa5878` |
| D10 | gt_caused renamed gt_caused_heuristic | `0caa5878` |

### Substrate-reproof merge + revert
- Merged Go/Rust LSP env fixes from `codex/substrate-reproof` branch (`7b76d6ad`)
- **BROKE all Py/TS/JS** — `ModuleNotFoundError: No module named 'groundtruth.runtime.ledger'`
- Root cause: Docker GHA cache served stale `COPY src` layer + `gt_mini_patch.py` imports `groundtruth.runtime.*` at `.pth` bootstrap before `/opt/gt/src` is on sys.path
- Reverted merge (`a2119a8b`)
- Added cache-bust ARG before COPY src (`4e97b9eb`)
- Added graceful import fallbacks for all runtime modules (`00bd27fd`)
- Added `/opt/gt/src` to sys.path at module top (`faf8c6b1`)

### Go/Rust fixes
- Go: bumped from 1.22.5 to 1.24.4 (`cb3a6530`, `31aad5b6`)
- Go: GOFLAGS changed from `-mod=mod` to `-mod=readonly` (`cb3a6530`)
- Go: gomodcache mounted writable (was :ro) (`468164cd`)
- Go: workspace probe uses `go list -e` + `-mod=mod` + live GOPROXY (`d8fe8b37`)
- Rust: baked cargo+rustc+rust-src into substrate (`31aad5b6`)
- Rust: rust_src removed from hard requirement in dep_store_manifest (`468164cd`)
- Rust: sysroot backfill from task image (`31aad5b6`)
- Dep manifest non-fatal for missing deps (`cb3a6530`)

### Pipeline architecture documented
- `GT_PIPELINE_ARCHITECTURE.md` — three surfaces, what goes where and why
- `gt_gt.md` §17 — D1-D10 closure, pipeline architecture, run results

### Validation runs

| Run | Substrate | Py/TS/JS | Go | Rust | Notes |
|---|---|---|---|---|---|
| `27367976952` | `2595578534` (pre-D1-D10) | 5/5 pass (0 resolved) | 0/2 proof fail | 0/2 proof fail | Baseline |
| `27435048257` | `aa3e8908` (merged reproof) | 0/5 adapter fail | 0/2 proof fail | 0/2 proof fail | REGRESSION — reverted |
| `27439536464` | `e89eccdd` (cache-busted) | 5/5 pass (0 resolved) | 0/2 proof fail | 0/2 proof fail | Cache bust worked for Py/TS/JS |
| `27443081763` | `e5f9c7c5` (Go 1.24.4+Rust) | N/A | 0/2 proof fail | 0/2 proof fail | Go .lock readonly, Rust rust_src required |
| `27443884077` | `e5f9c7c5` (same) | N/A | 0/2 probe fail | 0/2 manifest fail | Writable mount didn't help — -mod=readonly + GOPROXY=off |

### Test results from run `27439536464` (the clean Py/TS/JS run)

| Task | Language | Steps | Tests Passed | Tests Failed | Gap |
|---|---|---|---|---|---|
| adaptix | Python | 186 | 2815 | 1 | **1 test away from flip** |
| aiomonitor | Python | 85 | 12 | 1 | **1 test away from flip** |
| awilix | TypeScript | 47 | 1 | 24 | Variable scoping bug — D6 retry would catch |
| csstree | JavaScript | 97 | 0 | 14 | Incomplete implementation |
| katex | JavaScript | 112 | 22 | 95 | Regression — parser restriction too strict |

Trajectories saved at: `.claude/reports/runs/validation_27439536464/`

## What's building now

Substrate `27444631131` with:
- Go 1.24.4 + `go list -e ./...` + `-mod=mod` + live GOPROXY for workspace probe
- Rust: cargo+rustc+rust-src baked, rust_src non-required in manifest
- Cache-bust on COPY src

## Next session: dispatch and audit

### Step 1: Get the digest from build `27444631131`
```bash
gh run view 27444631131 -R hbali-stack/groundtruth --log | grep -oP 'ghcr.io/hbali-stack/gt-substrate@sha256:[a-f0-9]{64}' | tail -1
```

### Step 2: Dispatch ONLY the 5 failed tasks
```bash
gh workflow run 289588761 -R hbali-stack/groundtruth --ref gt-trial \
  -f model="deepseek/deepseek-v4-flash" \
  -f instance_ids="abs-module-cache-flags,abs-stepped-slices,boa-hierarchical-evaluation-cancellation,fd-deterministic-multi-key-sorting,arktype-json-schema-refs-dependencies" \
  -f max_parallel=5 \
  -f gt_substrate_digest="<DIGEST>" \
  -f require_pinned_substrate=1
```

### Step 3: If Go/Rust pass → combine with saved Py/TS/JS results
The Py/TS/JS trajectories from run `27439536464` are saved. Combine both runs for the full 10-task picture.

### Step 4: gt_trial §4 audit
Read trajectories chronologically. Per-layer tables. Focus on:
- Did the D6 retry feedback fire on any task?
- Did the pre-submit obligation (D3) fire at >90% budget?
- Did the state-aware dedup deliver fresh obligations after new edits?
- What context was the agent missing for the 1-test-away tasks (adaptix, aiomonitor)?

### Step 5: If audit is clean → full 113-task benchmark

## Key files

| File | What |
|---|---|
| `GT_BUGFREE_STATUS_AND_BUILD_PLAN.md` | Full status + 10 architectural pieces |
| `GT_PIPELINE_ARCHITECTURE.md` | Three-surface boundary rules |
| `gt_gt.md` §17 | D1-D10 + pipeline architecture |
| `NEXT_SESSION_PROMPT.md` | Execution prompt (updated) |
| `.claude/reports/runs/validation_27439536464/` | Saved Py/TS/JS trajectories |
| `.claude/reports/runs/validation_27367976952/` | Prior run trajectories + handoff docs |

## Critical insight

**adaptix and aiomonitor are each 1 test away from flipping.** The agent writes 2815/2816 correct tests for adaptix. The missing piece: the agent doesn't know its patch has a bug. D6 (retry feedback) is built but the retry loop reported `status=pass` because the agent's OWN test suite passes — the failure is only visible to the official hidden verifier.

The path to flips: obligation specificity. If GT tells the agent "you edited alias mapping but haven't tested alias collision with existing field names" (the exact requirement the hidden test checks), the agent tests that edge case and catches its own bug.

## Commits this session (chronological)

```
77dc857c fix(lipi-d1d2d3): budget dedup post-gate, oracle reset, pre-submit boost
8d505603 fix(lipi-d4d5d6d7): tracker ratchet, templates fixed, ledger deferred
7da50622 fix(lipi-d1d2d7-followup): budget commit scoped to winner, dead reset removed, decay fixed
e7fd256e fix(d6): split env regex — agent-caused failures now get retry feedback
d7979a4e fix(d5): caller-direction witness emits callee name, not caller name
0caa5878 fix(d8-d10): phase thresholds data-derived, gt_caused renamed heuristic
7b76d6ad merge: integrate Go/Rust LSP env fixes (BROKE Py/TS/JS — reverted)
a2119a8b Revert "merge: integrate Go/Rust LSP env fixes"
53ae4106 docs: GT pipeline architecture
28514a3f docs(gt_gt): add §17
4e97b9eb fix(substrate): cache-bust COPY src layer
faf8c6b1 fix(bootstrap): add /opt/gt/src to sys.path
00bd27fd fix(bootstrap): graceful fallback for all runtime imports
cb3a6530 fix(substrate): Go 1.24.4 + GOFLAGS readonly + non-fatal Rust dep manifest
31aad5b6 fix(substrate): bake Rust toolchain + sysroot backfill
468164cd fix(go-rust): writable gomodcache + rust_src non-required
d8fe8b37 fix(go-probe): go list -e with mod=mod and live GOPROXY
```
