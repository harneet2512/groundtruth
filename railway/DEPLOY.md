# Railway LIVE smoke — OH+GT on `beetbox__beets-5495` (local runtime, no Docker)

One Railway service that runs the OpenHands + GroundTruth agent loop on ONE
SWE-bench-Live task, with OpenHands in `runtime=local` (bash executes IN the
Railway container — no Docker-in-Docker). Logs stream so you watch GT inject and
the agent act live. The gold-test EVAL runs **off Railway** on the emitted
`output.jsonl` (standard SWE-bench-Live harness).

Project already exists: **gt-live-smoke**, id `77a823bd-aa73-406c-b7b5-5bf1f5ba8db7`.

---

## What runs

- Image: `python:3.12-slim` + git + build-essential + Go 1.22.
- OpenHands **0.54.0 from source** (`/opt/OpenHands`, `pip install -e .`) — the
  `evaluation.*` modules and the `LocalRuntime` registry live in the monorepo,
  not the `openhands-ai` pip wheel.
- GroundTruth (`src/`, `scripts/`, `gt-index/`) `pip install -e .`.
- beets cloned at base commit `fa10dcf11add0afd3b4b22af29f8d504e7ef8a0a` into
  `/workspace/beetbox__beets-5495`, `pip install -e .` + `responses`, `pytest`.
- `graph.db` **built at image-build time** by the `gt-index` Go binary (also
  built in the image). See "graph.db strategy" below.
- Entry point: `railway/entrypoint.sh` (GUARDED — see below).

The agent loop itself is `railway/run_one_task.py`: a standalone local driver
(NOT the SWE-bench Docker eval harness) that builds a `LocalRuntime`, installs
the GT hooks via the production wrapper's `patched_initialize_runtime`, runs
`run_controller`, and writes `output.jsonl`.

---

## Step-by-step

All commands run from the GT repo root (`D:\Groundtruth`). Install the Railway
CLI first (`npm i -g @railway/cli` or `scoop install railway`), then `railway login`.

### 1. Link the existing project

```powershell
railway link --project 77a823bd-aa73-406c-b7b5-5bf1f5ba8db7
```

(or `railway link` and pick **gt-live-smoke** interactively.)

### 2. Set the secret (operator only — never in any file)

```powershell
railway variables --set "DEEPSEEK_API_KEY=sk-..."
```

This is the only secret. No key is baked into the image or any committed file.
`config.toml` has a `${DEEPSEEK_API_KEY}` placeholder; the driver overrides it
from the env var at runtime.

### 3. First deploy = GUARDED validation (near-zero cost)

`RUN_SMOKE` is unset, so the entrypoint runs **only the env self-check** and
exits 0 — no LLM task, no spend beyond build minutes. This proves the image is
sound (OpenHands imports, `runtime=local` resolves, GT wrapper imports, beets
workspace + graph.db present, DeepSeek auth probe returns HTTP 200).

```powershell
railway up
railway logs            # read the "--- ENV SELF-CHECK ---" block; want "SELF-CHECK RC=0"
```

If the self-check fails, fix the image before spending. Do NOT set RUN_SMOKE=1
on a red self-check (the entrypoint refuses anyway).

### 4. Run the actual task (paid)

Set `RUN_SMOKE=1` and redeploy. Now the entrypoint runs the OH+GT agent loop.

```powershell
railway variables --set "RUN_SMOKE=1"
railway up
railway logs -f         # STREAM: watch GT inject (<gt-...> blocks) + agent edits live
```

The container is one-shot (`restartPolicyType: NEVER` in `railway.json`): when
the agent loop finishes it exits and **Railway stops billing**. It does not
restart.

### 5. Pull artifacts + EVAL off Railway

`output.jsonl` and `gt_interactions_*.jsonl` are written to
`/workspace/results/` inside the container and echoed (patch preview) in the
logs. To score the gold test, run the standard SWE-bench-Live harness on the
prediction **on your own machine / a Docker host** — NOT on Railway:

```bash
# off-Railway, on a Docker host:
python scripts/swebench/convert_to_submission.py results/output.jsonl --output-dir merged
python -m swebench.harness.run_evaluation \
    --dataset_name SWE-bench-Live/SWE-bench-Live --split lite \
    --namespace starryzhang \
    --predictions_path merged/predictions.jsonl \
    --max_workers 1 --run_id railway_smoke
```

To get `output.jsonl` off the container, either add a `railway run cat
/workspace/results/output.jsonl` step, mount a volume, or have the driver POST
it somewhere — Railway one-shot containers don't persist a filesystem after exit,
so capture from `railway logs` (the patch is previewed there) or attach a volume
at `/workspace/results` if you need the full file.

---

## graph.db strategy (chosen: build at image-build time)

The GHA canary builds `graph.db` at runtime via `gt-index` + an offline pre-index
from the SWE-bench Docker image. Railway has no Docker, so instead:

**Chosen:** build `gt-index` (Go/CGO) inside the image, then run it on the
already-checked-out beets repo at build time, baking
`/workspace/beetbox__beets-5495/graph.db` into the image. Reproducible, no
Docker-in-Docker, no blob upload.

**Fallbacks (in priority order, all handled):**
1. If the Go build fails at image build, the image still builds; the entrypoint
   retries `gt-index -root ... -output graph.db` at start.
2. If `gt-index` is unavailable entirely, drop a prebuilt `railway/graph.db` in
   (built elsewhere on Linux for beets@base_commit) — the `.railwayignore` has
   `!railway/graph.db` so it uploads, and adjust the Dockerfile to `COPY
   railway/graph.db /workspace/beetbox__beets-5495/graph.db`.
3. With no graph.db at all, GT's graph-dependent layers (callers/impact) degrade
   to their fallbacks; contract/sibling/test layers still fire.

This does NOT use the offline LSP-promotion pass the canary runs (that needs the
Docker image's `/testbed`); name_match edges get the standard confidence
discount. Acceptable for a single-task live smoke.

---

## Cost notes

- **Railway**: the $5 plan covers build minutes + the one-shot run. The agent
  loop is the only LLM spend (DeepSeek, billed to your DeepSeek account, not
  Railway). One-shot + `restartPolicyType: NEVER` means no idle billing.
- **GHA minutes**: not consumed — this path is entirely Railway, separate from
  the `canary_3arm.yml` GHA flow.
- **EVAL**: runs off Railway (your Docker host); no Railway/GHA cost.

---

## TOP RISKS (read before first `railway up`)

1. **#1 RISK — OH 0.54 local-runtime + GT hook integration end-to-end.** The
   feasibility verdict is FEASIBLE_WITH_LOCAL_DRIVER, verified from 0.54.0 source
   (the registry has `'local' -> LocalRuntime`; `run_controller(config,
   initial_user_action, runtime=...)` exists; `LocalRuntime` inherits the
   `run_action`/`copy_to`/`copy_from` the GT hooks monkeypatch). But the exact
   `create_runtime()` / config-object call shapes in 0.54 are point-build
   sensitive — `run_one_task.py` tries several signatures and degrades, yet the
   FIRST `railway up` with `RUN_SMOKE=1` is the real proof. Expect to iterate
   on `build_config()` / `create_runtime()` arg shapes. The GUARDED self-check
   catches import/registry breakage cheaply before any spend.

2. **LocalRuntime spawns an action-execution server subprocess** that binds a
   local TCP port and needs `tmux`/`bash` available. `python:3.12-slim` has bash
   but NOT tmux; if 0.54's LocalRuntime requires tmux/libtmux, add `tmux` to the
   apt install. (libtmux is a pip dep of openhands-ai; the binary may still be
   needed.) Watch the self-check / connect step for a tmux error.

3. **beets editable install at the base commit may fail** on slim (missing
   system libs for some extras). The Dockerfile falls back across `pip install
   -e .` variants and continues; the agent can still edit pure-Python files even
   if some optional extras don't build. If imports the task needs are missing,
   add the apt deps (e.g. `libchromaprint-tools`, `ffmpeg`) — but the beets-5495
   fix is in pure-Python config handling, so this is likely fine.

4. **gt-index CGO build inside slim** needs `gcc` + `build-essential` (present)
   and network to fetch Go modules. If go-sqlite3 fails to compile, graph.db is
   not baked → graph layers degrade (non-fatal). Self-check warns, doesn't fail.

5. **`evaluation` namespace import.** The driver imports OH from `/opt/OpenHands`
   (PYTHONPATH) so `import openhands` resolves to the 0.54 source, not any other
   wheel. If a different `openhands-ai` wheel is also installed, version skew
   could shadow the registry — the image installs ONLY the 0.54 source, so this
   is controlled, but confirm `[check] openhands import OK -> /opt/OpenHands/...`
   in the self-check.

6. **DeepSeek model name.** Config uses `deepseek/deepseek-v4-flash` (litellm
   provider-prefixed) with `base_url=https://api.deepseek.com`. The auth probe
   uses the bare `deepseek-v4-flash`. If the probe 200s but the agent run gets a
   model-not-found, reconcile the model string (litellm prefix vs raw).

7. **Build context upload size.** `.railwayignore` excludes the GBs of `.tmp_*`,
   `results/`, caches. If `railway up` still uploads a lot, check for new scratch
   dirs not covered and add them.

8. **No filesystem persistence after one-shot exit.** Capture `output.jsonl`
   from `railway logs` (patch is previewed) or attach a Railway volume at
   `/workspace/results` before the paid run if you need the full artifact file.

---

## Files in this scaffold

| File | Purpose |
|---|---|
| `railway/Dockerfile` | Build the OH 0.54 + GT + beets image, bake graph.db |
| `railway/config.toml` | OH config, `runtime=local`, `[llm.eval]` DeepSeek |
| `railway/entrypoint.sh` | Guarded: self-check (RUN_SMOKE!=1) or agent loop (==1) |
| `railway/run_one_task.py` | Standalone local driver (LocalRuntime + GT hooks + run_controller) |
| `railway/railway.json` | One-shot service (restart NEVER), Dockerfile build |
| `railway/.railwayignore` | Trim the `railway up` upload |
| `railway/DEPLOY.md` | This file |
