# GO / NO-GO — DeepSWE GT integration

> Branch `gt-trial`. Gates whether DeepSWE GT runs may launch. Detail: `docs/DEEPSWE_GT_*`.
> **DeepSWE integration is NOT complete** — adapter + workflow exist only as an uncommitted draft; D0–D4 unrun.

## Current verdict
| Run type | Status | Reason |
|---|---|---|
| DeepSWE local doc audit | **YES** | the 5 docs are complete (this pass) |
| DeepSWE 1-task dry/proof run | **NO** | substrate digest unpublished; **only pyright baked** (70% non-Python = `LSP_INSTALL_MISSING`); **e5 baked, loader defaults gte** (fail-close mismatch); adapter draft unvalidated (D0/D1 unrun) |
| DeepSWE held-out 10 | **NO** | depends on D1+D2 passing |
| DeepSWE paid/full run | **NO** | depends on D0–D3 + a frozen GT-off baseline |

## What must be TRUE before flipping the 1-task run to YES
1. `Dockerfile.gt-substrate` rebuilt baking **gopls + rust-analyzer + typescript-language-server** (+ **gte int8** or pin `GT_EMBED_MODEL_NAME=e5`); image published to GHCR; `GT_SUBSTRATE_DIGEST` pinned.
2. The DeepSWE adapter draft (gt_agent/gt_mini_patch witness + consume) committed + the `graph_certificate` import guarded.
3. D0 + D1 pass.

## DeepSWE validation stages
### D0 — GHA/code map
Pass: DeepSWE runner entrypoint found (`pier` + `artifact_deepswe/gt_agent.py:GTMiniSweAgent`) · workflow found (`deepswe_full.yml`) · task repo location (`/tmp/gt/src` → `/work:ro`) · artifact location (`/gt_artifacts`=`/tmp/gt`) · agent start (the pier step) · **pre-agent GT insertion point (the new substrate step, before pier)** · language distribution (TS35/Go34/Py34/Rust5/JS5). **STATUS: DONE (this audit).**

### D1 — local portable substrate run
Pass: pinned digest used · `/work:ro` works · `/gt_artifacts` generated · all 7 certs exist · no runtime model download · no pip install · **language classified** (per the multilang matrix). **STATUS: NO** — needs the rebuilt+published image; pyright-only today.

### D2 — one DeepSWE official task dry run
Pass: pinned digest · no host GT exec · artifacts produced · adapter consumes (witness `hook_graph_hash==post-LSP`, `gt_prebuilt_active=true`) · **language/LSP status classified** · prediction produced · artifacts uploaded. **STATUS: NO** — adapter draft unvalidated; pier→container env passthrough unverified.

### D3 — held-out 10
Pass: 10/10 produce or classify GT artifacts · 0 silent fallbacks · 0 host GT exec · clean failure classes · **language distribution reported**. **STATUS: NO.**

### D4 — larger run
Only after D0–D3. **STATUS: NO.**

## Is Stage 1 implementation safe to begin?
**Partly.** The GT-code side of Stage 1 is REUSE (no change). The **safe, non-risky first implementation
is the Docker/substrate change** (bake gopls/rust-analyzer/tsserver + gte) + publishing/pinning the
digest — it's additive, generalized, no benchmark logic, and it unblocks 70% of tasks + the embedder
mismatch. The DeepSWE-pipeline adapter is already drafted (uncommitted) and needs committing +
import-guard + D0-D2 validation before it's trusted. **Do NOT launch any run until the image is rebuilt
+ pinned and D1 passes.**

## Exact files to touch first (if implementation approved)
1. `docker/Dockerfile.gt-substrate` — add `gopls`, `rust-analyzer`, `typescript-language-server`; add gte int8 (or keep e5 + pin). Rebuild via `gt_substrate_image.yml`; capture the new digest.
2. `artifact_deepswe/gt_agent.py` — guard the `scripts.metrics.graph_certificate` import (PYTHONPATH-independent); commit the witness emitter.
3. `artifact_deepswe/gt_mini_patch.py` — confirm `GT_HOST_GRAPH_DB` read + L6-gated-off-in-substrate.
4. `.github/workflows/deepswe_full.yml` — commit the draft substrate step; set `GT_SUBSTRATE_DIGEST`; add a dry/proof-only mode.
5. `scripts/verify/deepswe_outcome.py` — add infra/GT/agent classification (later, Stage 5).

## Legitimacy (enforced)
No task edits / gold / FAIL_TO_PASS / test-name leakage / per-task or per-repo exceptions / gate-or-ranking
tuning to known tasks / hidden host GT exec / substrate→host fallback in proof / prebuilt graph from
outside the task repo / mutable image tag as proof input. All artifacts per task; all failures classified.
