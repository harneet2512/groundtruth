# SWE Live Lite + OpenHands ↔ GroundTruth Integration Handoff

> **Status:** handoff + implementation-discipline contract for the full-runtime structural
> legitimacy proof. **No behavior changes are introduced by this document.**
>
> **Baseline commit (full-runtime proof, before any new Phase 1–5 work):** `2a4f965a`
> on branch `gt-trial`. The working tree had **no tracked uncommitted proof-work changes** at
> handoff time — `2a4f965a` already contains the current proof work (graph-handoff fix,
> deliver-always, legitimized host handoff, `eval_no_report` INFRA classification). The
> untracked entries in the tree are pre-existing reports / old benchmark bundles and are **not**
> part of this proof; they are intentionally excluded from any commit here.

---

## 0. Why this document exists

GroundTruth (GT) is **one product with shared base/runtime layers** that must behave identically
no matter which agent harness or benchmark drives it. SWE-bench-Live "Lite" is run through an
**OpenHands (OH)-specific integration**. The two concerns must be kept separate so that, when the
full-runtime proof fails somewhere, we can tell **which surface** failed — GT architecture,
containerization, OH integration, the final GHA pipeline, the image cache, or the test harness.

This doc fixes (1) the shared-vs-OH split and (2) the staged implementation discipline that all
further full-runtime proof work must follow.

---

## 1. SWE Live Lite + OH integration handoff

### 1.1 The split — shared GT base layers vs OH-specific integration

**Base GT runtime layers (SHARED across all pipelines).** These are the substance of the proof
(Stages 1–5) and must be identical regardless of harness:

- **Graph-base depth** — `gt-index` builds FTS5 + CALLS + trust tiers + properties/`data_flow` +
  closure; `resolve.py` LSP-enriches edges; closure is rebuilt **after** LSP.
- **Embedder usage** — the ONNX e5 embedder is loaded from the baked model path **and actually
  consumed** by every semantic scoring path (`run_v74`, `localize`, `v1r`/render, gates).
- **LSP liveness / timing** — the language server launches, completes a **real warm probe**,
  enriches the graph **before** scoring/rendering, and triggers a closure rebuild.
- **Proof-mode fail-closed** — under `GT_PROOF_MODE=1` the 8 flags
  (`GT_REQUIRE_FTS5/EMBEDDER/LSP/FULL_STACK`, `GT_FORCE_ONNX_EMBEDDER`, `GT_CONTAINERIZED`,
  `GT_FORBID_PREBUILT_GRAPH`, `GT_PROOF_MODE`) hard-fail any partial/degraded run.

These layers live in GT code (`src/groundtruth/…`, `scripts/metrics/foundational_gates.py`) and
emit certificates/contracts that are **harness-agnostic**.

**OH-specific integration (DIFFERS per pipeline).** SWE Live Lite drives GT through OH plumbing:

- the **OH wrapper** (`scripts/swebench/oh_gt_full_wrapper.py`),
- the **agent hooks** (post-edit L3, post-view L3b, auto-query L4, reindex L6),
- the **`output.jsonl`** observation logs,
- **artifact extraction** (contracts/certs out of the container),
- the **eval path** (grader, predictions JSONL),
- the **GHA workflow plumbing** (`.github/workflows/swebench_300task.yml`).

### 1.2 OH integration — EXTRA proof requirements (on top of the shared base)

For SWE Live Lite/OH the shared base certificates must hold **and additionally**:

- the OH wrapper must **READ the resolved graph**, not rebuild a separate, unresolved one;
- hook logs must show the **same graph hash/path** as the post-LSP graph;
- `[GT_META] host_resolved_graph_db=…` must be present in the agent log;
- `_gt_prebuilt_active=True` must be present when the resolved graph is handed off to the hooks;
- `gates_only` and live mode must share the **same graph/brief prefix**;
- predictions / eval artifacts must be **unique by `instance_id`** and complete.

### 1.3 Note for the other (OH) session

**Reuse the base GT runtime work; audit the OH adapter separately.** Do **not** duplicate GT logic
inside the OH wrapper. The wrapper **consumes** GT-produced artifacts/certificates (the LSP,
graph-handoff, and embedder certificates) — it does **not** recreate them. The shared base proof
(Stages 1–3) is authored once in GT code; the OH session only proves that the OH adapter delivers
those artifacts to the agent unchanged.

---

## 2. Main Pipeline Implementation Discipline / Repo Hygiene

This applies to the main pipeline **before** implementing any more full-runtime proof work.

### 2.1 Why this matters

The full-runtime proof touches GT code, container runtime, GHA workflows, image cache, gates,
hooks, and tests. If implemented as one giant diff, we will not be able to tell whether a failure
came from: GT architecture · containerization · SWE-Live-Lite/OH integration · final pipeline/GHA ·
image cache · test harness. **Therefore implementation must be staged and committed by surface.**

### 2.2 Pre-implementation checkpoint

Before implementing new Phase 1–5 work, make the repo reviewable. Run:

```bash
git status --short
git diff --stat
git diff --name-only
```

- If workflows changed, run a YAML parse check.
- If Python files changed, run `python -m py_compile` on the touched Python files.
- If cheap and relevant, run the existing proof-surface tests.

If there are existing uncommitted changes from the current main/proof work, **commit them first as
a baseline commit**:

- commit message: `checkpoint: current full-runtime proof baseline`
- include only already-existing work
- do **not** include new Phase 1–5 implementation in this baseline commit
- after committing, record the baseline commit SHA in this handoff doc

> **Checkpoint result at handoff (`2a4f965a`):** no tracked uncommitted proof-work changes existed,
> so **no baseline commit was created** — `2a4f965a` is the baseline. (Recorded above, §0.)

### 2.3 Required staged commits

Do **NOT** implement Phases 1–5 as one giant diff. Implement stage-by-stage only.

| Stage | Commit message | Scope | Purpose |
|---|---|---|---|
| **0** | `audit: map final GT runtime path` | `PHASE0_RUNTIME_MAP.md`; no behavior change except harmless diagnostics | Document the exact current final path before changing it |
| **1** | `proof: enforce LSP liveness before gate pass` | LSP liveness cert (`resolve.py`) + `foundational_gates.py` LSP verdicts + LSP tests | Prevent fake LSP passes (`residual==0` without a warmed server) |
| **2** | `proof: certify resolved graph handoff` | graph-base depth cert + resolved-graph handoff cert + hook graph-hash witness + graph tests | Prove the same post-LSP graph is used by build, gates, and OH hooks |
| **3** | `proof: enforce embedder usage across semantic paths` | embedder usage cert + `run_v74`/`localize`/`v1r` embedder identity + semantic usage hard gates + embedder tests | Prove the embedder is consumed by every semantic scoring path, not merely installed |
| **4** | `pipeline: run LSP and gates inside eval container` | move LSP + gates into the eval container + lock proof mode to container execution + container-lockdown tests (no cache changes yet) | Kill the host/image split in proof/final mode |
| **5** | `pipeline: enforce full300 image cache and manifest contract` | image cache + final-pipeline determinism + manifest consistency + image digest/GHCR enforcement + pipeline/cache tests | Make the 300-task workflow deterministic and tied to the same cached image/task manifest |

**Stage 0 must include:** gates_only vs live path comparison · host/substrate path comparison ·
graph/LSP/embedder/gates execution location · artifact upload path · where OH hooks consume GT
artifacts · whether each step uses the same `source_root`, `graph_db`, `models_root`,
`runtime_context_id`.

### 2.4 After each stage

After each stage, show:

1. files changed
2. exact invariant added
3. tests added
4. tests run and results
5. `py_compile` result where relevant
6. YAML parse result where relevant
7. updated `GO_NO_GO.md`
8. remaining stages
9. whether this stage blocks: 300 dry · 30 live · full 300

**If any structural gate fails, stop and report. Do not continue to the next stage.**

### 2.5 Permission rule

Do not bypass permissions. Use manual approval for edits. This is not a small refactor — the
implementation touches workflows and proof semantics, so every stage must be reviewable.

### 2.6 Legitimacy rules

Do **not**:

- make task-specific fixes
- edit SWE-bench tasks
- use gold patches
- use FAIL_TO_PASS
- use test names
- tune ranking/gates to known tasks
- relax proof flags
- run 300 dry or live until the staged local gates pass
- declare success without GHA artifacts

---

## 3. GO / NO-GO

A live full-300 run is permitted only after Stages 1–5 are committed cleanly and the staged
validation runs (local → 1-task → held-out-10 → 300-dry → 30-live) pass structurally. The running
GO/NO-GO state is tracked in `GO_NO_GO.md`; this document is the discipline contract that governs
how we get there.
