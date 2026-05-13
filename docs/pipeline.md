# SWE-bench Eval Pipeline Optimizations

## Problem
Each eval job (stage 2 has 5, stage 3 has 20) repeats identical setup:
- Cache pip → Cache OH → Install OH → Cache gt-index → Build gt-index → Install GT
- ~3 min per job = 60 job-minutes wasted on setup for 20 parallel jobs

## Optimization 1: Composite Action (SHIPPED)

**File:** `.github/actions/setup-eval/action.yml`

Extracts all 6 setup steps into one reusable composite action. Each eval job calls:
```yaml
- uses: ./.github/actions/setup-eval
```
instead of 6 separate steps.

**Benefits:**
- Eliminates 122 lines of YAML duplication
- Skip logic: checks `python -c "import openhands"` before running pip install
- When caches hit, OH install drops from 81s to ~2s

## Optimization 2: Pre-baked Docker Image (READY, NOT DEPLOYED)

**Files:**
- `.github/docker/Dockerfile.eval-runner` — Image with OH + GT + gt-index pre-installed
- `.github/workflows/build_eval_image.yml` — Builds and pushes to `ghcr.io/harneet2512/gt-eval-runner:latest`

**How it works:**
- Build once → push to GHCR
- Eval jobs pull the image (~10s) instead of installing everything (~3 min)
- Needs Docker-in-Docker for OH eval (which spawns containers) — TBD if GHA supports this

**NOT deployed yet** because GHA `container:` + Docker-in-Docker is complex. The composite action handles the immediate need.

## Optimization 3: Future — Parallel Image Pull + Eval

Currently each job pulls the SWE-bench task Docker image sequentially. Could pre-pull images in a prep job and share via cache/artifact. Low priority — image pulls are ~20s each.

## Current Pipeline Flow

```
Trigger → Stage 1 (1 task)
           ├─ GCP Auth (Workload Identity Federation)
           ├─ Setup (composite action: pip + OH + gt-index + GT)
           ├─ Canary (Vertex MaaS)
           ├─ Eval (100 iterations)
           ├─ Harness (Microsoft SWE-bench-Live)
           └─ Gate (cost, reasoning, patches)
         → Stage 2 (5 tasks, parallel)
           ├─ Same setup per job
           └─ Gate (cost, source edits, resolved)
         → Stage 3 (20 tasks, parallel)
           ├─ Same setup per job
           └─ Gate + Summary
```

## Cost Controls
- Budget kill-switch: $50/month (Pub/Sub topic, alerts at 50/75/90/100%)
- Per-call logging: `[GT_COST]` lines in stdout with running total
- Per-task logging: `litellm_costs.jsonl` artifact
- No idle resources (0 VMs, 0 disks)
