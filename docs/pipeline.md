# SWE-bench Eval Pipeline

## Architecture

```
Trigger (workflow_dispatch)
  │
  ├─ Inputs: gt_commit, max_iterations, run_name, start_stage, model_override
  │
  ▼
Stage 1 (1 hard task: cfn-lint-4023)
  ├─ GCP Auth (Workload Identity Federation, no stored keys)
  ├─ Setup (composite action — see below)
  ├─ Canary (Vertex MaaS direct curl)
  ├─ Write OH config (model_override → OpenRouter free; default → Vertex 480B)
  ├─ Pull Docker image (task-specific SWE-bench container)
  ├─ Run evaluation (oh_gt_full_wrapper.py → OH run_infer)
  ├─ LiteLLM cost summary
  ├─ Harness (Microsoft SWE-bench-Live eval)
  └─ Gate (cost, patches, reasoning)
       │
       ▼ (pass)
Stage 2 (5 diverse tasks, parallel jobs)
  ├─ Same per-job setup
  └─ Gate (resolved count, cost)
       │
       ▼ (pass)
Stage 3 (20 tasks, parallel jobs)
  └─ Final summary artifact
```

## GCP Infrastructure

| Resource | Details |
|----------|---------|
| Project | `GCP_OLD_PROJECT_PLACEHOLDER` (baliharneet0@gmail.com) |
| Auth | Workload Identity Federation: `github-pool` → `github-provider` → `gha-vertex@` SA |
| Budget | Kill-switch at $50/month (Pub/Sub alerts at 50/75/90/100%) |
| Idle cost | $0 (all VMs and disks deleted) |
| API | Vertex MaaS: `qwen/qwen3-coder-480b-a35b-instruct-maas` at `locations/global` |
| Pricing | $0.45/M input, $1.80/M output, automatic KV caching (cached input ~$0.045/M) |

## Setup Bottleneck + Fix

### Problem
Each GHA job gets a fresh Python environment. Even with pip wheel caching, `pip install -e .` for OpenHands (200+ dependencies) takes ~81 seconds. With 20 parallel jobs in Stage 3, that's 27 job-minutes wasted.

### Root Cause
The original skip logic (`python -c "import openhands"`) always fails because GHA caches source files and pip wheels but NOT the installed `site-packages` directory. Every job re-installs from cached wheels.

### Fix: Cache site-packages directly

**File:** `.github/actions/setup-eval/action.yml`

Single cache key covering everything:
```yaml
path: |
  ~/.cache/pip
  /tmp/OpenHands
  /tmp/gt-index
  /opt/hostedtoolcache/Python/3.12.*/x64/lib/python3.12/site-packages
  /opt/hostedtoolcache/Python/3.12.*/x64/bin
key: eval-env-oh054-gt-{hash of pyproject.toml + go files}
```

On cache hit: `import openhands; import groundtruth; import datasets` succeeds → skip all installs → setup drops from ~90s to ~5s (cache restore only).

On cache miss: full install runs, caches everything for subsequent jobs.

### Pre-baked Docker Image (backup, not deployed)

**Files:**
- `.github/docker/Dockerfile.eval-runner` — python:3.12-slim + OH + GT + gt-index
- `.github/workflows/build_eval_image.yml` — builds and pushes to GHCR

Not deployed because GHA `container:` + Docker-in-Docker (OH spawns containers) is unvalidated.

## Model Configuration

### Vertex MaaS (default)
```toml
model = "openai/qwen/qwen3-coder-480b-a35b-instruct-maas"
api_key = [GCP access token from WIF]
base_url = "https://aiplatform.googleapis.com/v1/projects/.../endpoints/openapi"
custom_llm_provider = "openai"
temperature = 0.7
top_p = 0.8
max_output_tokens = 65536
caching_prompt = false
```

### OpenRouter override (for $0 diagnostics)
Set `model_override` input to `openrouter/qwen/qwen3-coder:free` — auto-routes to OpenRouter with OPENROUTER_KEY secret.

### Sampling params (injected via monkey-patch)
OH's LLMConfig rejects `top_k` and `repetition_penalty` as extra fields. These are injected at the litellm call level by `scripts/swebench/cost_tracking.py`:
- `top_k = 20`
- `repetition_penalty = 1.05`
- Match condition: model string contains "qwen3-coder" AND "480b"

## Diagnostic Logging

Three log tags for payload investigation (first 3 LLM calls only):

| Tag | Source | What it captures |
|-----|--------|-----------------|
| `[GT_PAYLOAD]` | cost_tracking.py monkey-patch | Full litellm kwargs: model, extra_body, temperature, tools, drop_params, messages shape |
| `[GT_LLM_CONFIG]` | oh_gt_full_wrapper.py LLM.__init__ patch | OH config after __post_init__: reasoning_effort, enable_thinking, modify_params |
| `[GT_COST]` | cost_tracking.py success callback | Per-call cost, tokens, running total |

## Cost Controls

| Control | Details |
|---------|---------|
| GCP budget | $50/month kill-switch with Pub/Sub |
| Per-call log | `[GT_COST]` in GHA stdout (real-time) |
| Per-task log | `litellm_costs.jsonl` uploaded as artifact |
| Idle burn | $0 (0 VMs, 0 disks) |
| Free diagnostic | `model_override` input for $0 runs with free models |

## Known Issue: 0/6 Patches on GHA

**Status:** Under investigation. Payload-parity diagnostic in progress.

VM runs (oh_gt_live_lite_wrapper.py) → 3/30 resolves.
GHA runs (oh_gt_full_wrapper.py) → 0/6 resolves, 140 tok/call, reasoning=0.

Suspected causes (ranked by code evidence):
1. OH `__post_init__` sets `reasoning_effort='high'` — may confuse Vertex
2. `model` prefix `openai/` vs `vertex_ai/` — different litellm routing
3. `modify_params=False` on GHA but not VM — blocks litellm auto-transforms
4. Monkey-patch may not fire on OH's async path
5. OH may strip `extra_body` before the HTTP call

Diagnostic run captures `[GT_PAYLOAD]` and `[GT_LLM_CONFIG]` to resolve this.
