# SWE-bench Live Lite — GHA Operations Guide

## Workflows

| Workflow | Purpose | Trigger |
|----------|---------|---------|
| `Live Lite: Cache Docker Images` | Pre-cache 300 images to GHCR (one-time) | Manual |
| `Live Lite: Inference` | Run agent on tasks → produce predictions | Manual |
| `Live Lite: Evaluate` | Apply patches, run gold tests | Manual (after inference) |
| `Live Lite: Submission Bundle` | Accuracy gate + submission packaging | Manual (after eval) |

---

## First-Time Setup

### 1. Set secrets

In repo Settings → Secrets and variables → Actions:

| Secret | Required | Purpose |
|--------|----------|---------|
| `OPENROUTER_KEY` | Yes | LLM API key for agent inference |
| `DOCKERHUB_USERNAME` | Optional | Higher Docker Hub pull rate limits |
| `DOCKERHUB_TOKEN` | Optional | Docker Hub access token |

### 2. Pre-cache Docker images (one-time, ~2 hours)

```bash
gh workflow run "Live Lite: Cache Docker Images to GHCR" \
  -f batch_size=10 -f max_parallel=5
```

This caches all 300 SWE-bench Live Lite eval images from Docker Hub to GHCR. Subsequent inference/eval jobs pull from GHCR (same-network, fast, no rate limits). Run once — images persist indefinitely.

---

## Run Modes

### 1-task dev run (~30 min)

```bash
gh workflow run "Live Lite: Inference" \
  -f mode=smoke -f tasks_per_job=1 -f max_parallel=1 \
  -f run_id=dev-001
```

### 5-task smoke (~35 min inference + ~10 min eval)

```bash
gh workflow run "Live Lite: Inference" \
  -f mode=smoke -f tasks_per_job=1 -f run_id=smoke-001

# After inference completes:
gh workflow run "Live Lite: Evaluate" \
  -f run_id=smoke-001 -f eval_batch_size=25
```

### 20-task pilot (~1.5 hrs)

```bash
gh workflow run "Live Lite: Inference" \
  -f mode=pilot20 -f tasks_per_job=5 -f run_id=pilot20-001

gh workflow run "Live Lite: Evaluate" \
  -f run_id=pilot20-001 -f eval_batch_size=25
```

### 100-task pilot (~3.5 hrs)

```bash
gh workflow run "Live Lite: Inference" \
  -f mode=pilot100 -f tasks_per_job=5 -f run_id=pilot100-001

gh workflow run "Live Lite: Evaluate" \
  -f run_id=pilot100-001 -f eval_batch_size=50
```

### Full 300-task run (~7 hrs)

```bash
gh workflow run "Live Lite: Inference" \
  -f mode=full300 -f tasks_per_job=5 -f max_parallel=20 \
  -f run_id=full300-001

gh workflow run "Live Lite: Evaluate" \
  -f run_id=full300-001 -f eval_batch_size=50 -f max_parallel=6

gh workflow run "Live Lite: Submission Bundle" \
  -f run_id=full300-001 -f score_threshold=10
```

### Eval-only (rerun on existing predictions)

```bash
gh workflow run "Live Lite: Evaluate" \
  -f run_id=<prior_run_id> -f eval_batch_size=50
```

### Bundle-only

```bash
gh workflow run "Live Lite: Submission Bundle" \
  -f run_id=<prior_run_id> -f score_threshold=10
```

---

## Performance Breakdown

### Inference (the bottleneck)

```
Per job:   ~3 min setup (cached OH + parallel Docker pulls)
         + tasks_per_job × ~25 min inference
         ─────────────────────────────────
         = ~128 min at 5 tasks/job

Wall clock = ceil(jobs / max_parallel) × per_job_time
```

| Mode | Tasks | tasks/job | Jobs | Rounds | Per-round | Wall clock |
|------|-------|-----------|------|--------|-----------|------------|
| smoke | 5 | 1 | 5 | 1 | ~28 min | ~30 min |
| pilot20 | 20 | 5 | 4 | 1 | ~128 min | ~2.2 hrs |
| pilot100 | 100 | 5 | 20 | 1 | ~128 min | ~2.2 hrs |
| full300 | 300 | 5 | 60 | 3 | ~128 min | ~6.4 hrs |

### Eval (~30-40 min regardless of task count)

6 parallel jobs × 50 tasks × 4 Docker workers = all 300 tasks in ~30 min.

### Total pipeline

| Mode | Inference | Eval | Total |
|------|-----------|------|-------|
| smoke | ~30 min | ~10 min | ~40 min |
| pilot20 | ~2.2 hrs | ~15 min | ~2.5 hrs |
| pilot100 | ~2.2 hrs | ~25 min | ~2.5 hrs |
| full300 | ~6.4 hrs | ~35 min | **~7 hrs** |

---

## Optimizations Applied

| Optimization | Saves | How |
|---|---|---|
| Cached OpenHands clone | ~3 min/job | `actions/cache` on `/tmp/OpenHands`, skip clone on hit |
| Cached pip packages | ~2 min/job | `actions/cache` on `~/.cache/pip` |
| Cached gt-index binary | ~2 min/job | `actions/cache` on `/tmp/gt-index` |
| Parallel Docker pulls + pip install | ~2 min/job | Docker pulls run in background while pip installs |
| GHCR image cache (one-time) | ~2 min/job | Same-network pulls (~0.3s) vs Docker Hub (~1.5 min) |
| tasks_per_job=5 | 5 fewer rounds | 60 jobs in 3 rounds vs 150 jobs in 8 rounds |

**Net: ~3 min setup vs ~10 min uncached. ~7 hrs total vs ~8.5 hrs.**

---

## Artifacts

| Artifact | Created By | Retention |
|----------|------------|-----------|
| `inference-{run_id}-batch-{N}` | Inference | 30 days |
| `predictions-{run_id}` | Inference (merge) | 90 days |
| `eval-splits-{run_id}` | Eval (prepare) | 7 days |
| `eval-batch-{run_id}-{N}` | Eval | 90 days |
| `eval-results-{run_id}` | Eval (aggregate) | 90 days |
| `submission-bundle-{run_id}` | Bundle | 90 days |

---

## Submission Process

1. Run full300 inference + eval + bundle
2. Download `submission-bundle-{run_id}` artifact from the Actions tab
3. Clone `https://github.com/SWE-bench-Live/submission`
4. Copy the `submissions/lite/YYYYMMDD-{run_id}/` directory into the clone
5. Open a PR

---

## Troubleshooting

**Disk full during inference:**
Reduce `tasks_per_job` to 3 or 2. More tasks = more Docker images on disk simultaneously.

**Docker pull failures:**
Run `Live Lite: Cache Docker Images` workflow first. All subsequent pulls come from GHCR.

**Matrix exceeds 256:**
Increase `tasks_per_job`. At 5/job: 60 entries. At 7/job: 43. At 10/job: 30.

**Partial inference (some batches failed):**
Merge runs with `if: always()` and collects whatever succeeded. Rerun the workflow with same `run_id` — dedup handles overlapping results.

**Eval finds no result JSONs:**
Check eval job logs for actual harness output path. The aggregate job searches common patterns.
