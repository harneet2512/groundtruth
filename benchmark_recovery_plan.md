# Benchmark Recovery Plan — 2026-03-21

## Diagnosis

### What is BROKEN
1. **GT shards (both)**: `openhands_run_vertex_gt.sh` does NOT pass `--num-workers` to swebench-infer. OpenHands defaults to **30 workers** on an e2-standard-8 (8 CPUs). Result: load average 55, 523 image build failures, 376 runtime failures, only 2-3 successful outputs. These runs are totally wasted.

### What is SLOW but working
2. **Baseline shards (both)**: Running with 1 worker as intended. 24/150 (shard-a) and 22/150 (shard-b) completed in ~3 hours. Pace: **400-530s per task (~7-8 min)**. Zero failures. Clean outputs.

### What is the root cause of slowness vs "old 4-hour" runs
3. **Task turn count**: Tasks use 123-199 conversation turns each. At ~2-3s per Vertex API round-trip, that's 250-600s per task inherently. The old 4-hour runs used GPT-5-mini (much faster inference) or 8 workers (higher parallelism). Qwen3-Coder on Vertex via litellm proxy is slower per-turn.
4. **1 worker per shard**: Chosen to avoid image build contention, but too conservative. Baseline shows 0 build failures — we can safely run 3 workers on e2-standard-8.

### What is log contamination / noise
5. gt-shard-a `/tmp/run_shard.log` contains both old (env_setup) and new (prompt-only) run logs mixed together. The 523 build failures are mostly from the 30-worker meltdown.
6. gt-shard-b has similar mixed state.

### Throughput math
- **Current baseline**: 1 worker, ~450s/task, 150 tasks = **18.75 hours per shard**
- **With 3 workers**: ~450s/task / 3 = 150s effective, 150 tasks = **6.25 hours per shard**
- **Both shards parallel**: **6.25 hours total for 300 baseline tasks**
- **GT (if identical)**: **6.25 hours after baseline completes**

## Recovery Plan

### PHASE 1: STABILIZE (immediate)
1. **Kill GT shards** — both are wasting credits with 30-worker meltdowns
2. **Bump baseline to 3 workers** — baseline has 0 failures at 1 worker, images are now cached (47 on shard-a, 44 on shard-b), 3 workers is safe
3. **Preserve baseline Docker caches** — 47/44 images built, invaluable for GT rerun

### PHASE 2: BASELINE COMPLETION (~4 hours from worker bump)
- shard-a: 126 remaining / 3 workers / ~8 per hour per worker = ~5.25 hours
- shard-b: 128 remaining / 3 workers = ~5.3 hours
- Both parallel: **~5.3 hours from now**

### PHASE 3: GT FRESH RELAUNCH
- After baseline completes, reuse the SAME 4 VMs (warm Docker caches)
- baseline-shard-a → runs gt-shard-a tasks (same shard_a.txt)
- baseline-shard-b → runs gt-shard-b tasks (same shard_b.txt)
- Current gt-shard-a/b VMs: kill, keep for eval later (or delete)
- Fresh output dirs, fresh logs, 3 workers
- **Fix the GT script to pass --num-workers**
- Prompt-only mode (already fixed)

### PHASE 4: EVAL
- Run eval on a separate VM or on idle shard VMs after generation
- Eval takes ~30 min per condition

## Cost estimate
- 4 VMs x e2-standard-8 x ~12 more hours = ~$13 compute
- Vertex API: already ~$30 spent on baseline, ~$30 more for GT = $60 API
- Total remaining: ~$73

## Key fix needed
The GT launch script must pass `--num-workers` to swebench-infer. Currently it passes extra args but the baseline launch command uses `--num-workers 1` explicitly while the GT script does not.
