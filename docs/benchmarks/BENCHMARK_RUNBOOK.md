# BENCHMARK_RUNBOOK.md — how to run the GT benchmarks (legitimate, gated, parallel)

> Single operational source of truth for running the real benchmarks. Branch
> `gt-consensus-curation`. Last updated 2026-06-03.

The 30-task run `26909714974` silently degraded (FTS5 rebuilt, semantic=0, LSP 0ms) and
produced confounded results. Everything below exists so that can't happen again: a run
either proves the full stack is live and indexes legitimately, or it **aborts before
spending**.

---

## 0. The four invariants every paid run must satisfy

1. **Full stack LIVE, no silent fallback** — FTS5 (Go-built), ONNX semantic embedder,
   LSP (real launch + resolve), graph-base dimensions (data_flow enrichment, assertions,
   edge quality). Any degradation → hard abort.
2. **Legitimate indexing** — graph built FRESH per task, in the task env, from the
   base-commit repo, blind to the solution, with the shipped binary + passes. No
   prebuilt / cross-run graph.db.
3. **Parallel** — one task per job, fanned across the runner ceiling.
4. **Install once** — the toolchain is baked into the eval image, not re-installed per job.

---

## 1. Env knobs (what each gate does)

| Var | Effect | Where set |
|---|---|---|
| `GT_REQUIRE_FULL_STACK=1` | OH preflight + per-task graph-dim gate abort on any degraded dimension | OH workflows |
| `GT_REQUIRE_FULL_POTENTIAL=1` | DeepSWE `preflight_pipeline.py` hard-fails on degraded stack | DeepSWE workflows |
| `GT_REQUIRE_FTS5=1` | `gt-index` aborts indexing if `nodes_fts` absent/empty (no Python rebuild) | all |
| `GT_REQUIRE_EMBEDDER=1` | `_get_embedder`/`_get_model` RAISE if no embedder (no W_SEM=0) | all |
| `GT_FORCE_ONNX_EMBEDDER=1` | both semantic halves use the container ONNX `_OnnxEmbedderAdapter` (consistency) | all |
| `GT_REQUIRE_LSP=1` | wrapper aborts if LSP server doesn't launch; per-task resolve probe asserts `lsp_references` + latency>0 | OH only (DeepSWE uses offline `lsp_edges`) |
| `GT_FORBID_PREBUILT_GRAPH=1` | refuses any prebuilt/cross-run graph.db; forces fresh in-container index | 300 + DeepSWE |
| `GT_MODELS_ROOT` | points the embedder at the baked e5 model (set in the image) | image |
| `GT_EVAL_IMAGE=1` | tells `setup-eval` to skip re-installs (toolchain baked) | image |

---

## 2. The baked eval image (install once)

`.github/docker/Dockerfile.eval-runner` → `ghcr.io/harneet2512/gt-eval-runner:latest`.
Bakes: Python 3.12, **Go 1.23 (upstream)**, OpenHands 0.54, GT deps, **gt-index with
`-tags sqlite_fts5`**, 4 LSP servers (pyright, gopls, rust-analyzer, ts-language-server),
onnxruntime + e5 ONNX model (offline), pier, ripgrep, sqlite3, docker CLI.

**Build/refresh it (do this whenever the Dockerfile or deps change):**
- Dispatch **`build_eval_image.yml`** (workflow_dispatch). It pushes `:latest` to GHCR.

Jobs that run `container: ghcr.io/.../gt-eval-runner:latest` mount the host docker
socket (`--volume /var/run/docker.sock:/var/run/docker.sock`) so eval steps run task
images as **sibling** containers. Each job installs ONLY current GT (`pip install -e .`)
+ rebuilds `gt-index`, and forces `PYTHONPATH=$GITHUB_WORKSPACE/src:...` so the run uses
the **current** checkout, not the image's baked snapshot.

---

## 3. The workflows

| Workflow | What | Tasks | Parallel | Per-task | Whole run* |
|---|---|---|---|---|---|
| `canary_3arm.yml` | 2–3 tasks × 3 arms | 6–9 | 13 | ~16 min | ~25 min |
| `gt_v4flash_30.yml` | OH 30-task | 30 | 20 | ~16 min | ~30 min |
| `swebench_300task.yml` | **OH 300-task** | 300 | 20 | ~14 min (baked) | **~3.5 h** |
| `deepswe_full.yml` | **DeepSWE 113-task** | 113 | 20 (input) | ~17 min (baked) | **~1.6 h** |
| `deepswe_trial.yml` | DeepSWE single task | 1 | — | ~17 min | ~17 min |

\* At the ~20 concurrent GitHub-hosted runner ceiling on this plan. More runners → faster, linearly.

---

## 4. Run order (MANDATORY — validate before the paid run)

The container-job + docker-in-docker pattern is **not yet GHA-validated**. Do NOT launch
the full 113/300 first.

1. **Build the image** — dispatch `build_eval_image.yml`; confirm it pushes `:latest`.
2. **Validate 1 task each:**
   - DeepSWE: dispatch `deepswe_full.yml` with `max_tasks=1` (and/or `deepswe_trial` once it's containerized).
   - OH: dispatch `swebench_300task.yml` with its task-limit input set to 1 (or `gt_v4flash_30`).
   - In the job log confirm: graph builds in-container; `[GT preflight] FTS5 OK`; semantic
     ON (no "ONNX both unavailable"); `LSP resolve probe OK ... lsp_references`; the agent
     actually runs steps; `docker cp` paths work.
3. **Only if green → run the full set** with the desired `max_tasks` / `language`.

A degraded task aborts itself (you'll see `Refusing a degraded paid run`) — that's working
as intended, not a bug.

---

## 5. Legitimacy rule (the test for any indexing change)

> Would a real GT user — who opens this repo at its base commit, blind to the fix — get
> this exact graph from the shipped binary, in their own environment?

Yes → legitimate. Better-informed / better-promoted / cheaper-than-reality → gaming.
Per-task, fresh, base-commit, solution-blind, shipped binary+passes, no cross-run reuse.
`deepswe_preindex.yml` (cross-run artifact builder) is a binary-health SMOKE only — **never
the eval path**.

---

## 6. Known-open bottleneck

**DeepSWE task-image `docker pull`** — cold Docker Hub pulls per job (~1–3 min + rate-limit
risk); OH has a GHCR image-cache, DeepSWE does not. Next lever after the container is proven:
a DeepSWE GHCR image-cache workflow (mirror `cache_swebench_images.yml`).

---

## 7. Quick reference — commits this session (infra)

`51de7275` full-stack gates · `81eff53b` gate canary+30 · `bf8fd2b9` airtight LSP gate ·
`d879a1c2` per-task LSP resolve + 300 gate · `db867327` DeepSWE hard-gate · `e56784f6`
per-task graph-dim gate · `25d6eb50` legitimacy enforcement · `b0f957d7` DeepSWE 113 matrix
· `61db3b13` cap parallel at 20 · `cc2bd22a` correct eval image · `ed438843` both workflows
run in the baked image.
