# SWE-bench Lite Benchmark — Live Updates

**VM**: GCP `34.63.189.29` (8 vCPU, 32GB RAM, Ubuntu 22.04)
**Model**: gpt-5-mini | **Dataset**: princeton-nlp/SWE-bench_Lite (300 tasks)
**Config**: 16 workers, 30 turns, 600s timeout, 60s GT index timeout

---

## Latest Update: 2026-03-16 02:45 UTC

### Progress

| Condition | Completed | Patched | No Patch | Patch Rate | Est. Remaining |
|-----------|-----------|---------|----------|------------|----------------|
| Baseline | 64 / 300 | 58 | 6 | 90.6% | ~6-7 hours |
| GroundTruth | 38 / 300 | 28 | 10 | 73.7% | ~7-8 hours |

### API Cost (OpenAI)
- Baseline: ~$3.84 est (64 tasks x ~$0.06 avg)
- GroundTruth: ~$2.66 est (38 tasks x ~$0.07 avg)
- **Projected total**: ~$39 for both conditions combined

### GCP Cost
- Instance: e2-standard-8 (~$0.27/hr)
- Runtime so far: ~2 hours = ~$0.54
- **Projected GCP total**: ~$4.05 (15 hours total runtime)

### GT Bridge Stats
- Index timeouts (60s): 48 / 38 tasks attempted
- When indexing times out, task runs without GT (graceful degradation)
- Successful indexes: ~11s per repo (29K+ symbols for Django)

### Key Observations
1. Baseline has higher patch rate (90.6%) vs GT (73.7%) early on — GT agent spends turns on orient/brief/validate tools, fewer turns for actual edits
2. GT index timeouts happen when 16 workers try to index repos simultaneously (CPU contention)
3. Both runs are stable and progressing in batches of ~16 tasks every ~30 min

---

## Timeline

| Time (UTC) | Baseline | GT | Notes |
|------------|----------|----|-------|
| 00:09 | 0/300 started | — | Baseline Lite launched (first run, --no-resume) |
| 00:45 | 16/300 | — | First batch completed, all patched |
| 00:59 | 16/300 | 0/300 started | Baseline restarted with --resume, GT launched |
| 01:16 | 16/300 | 6/300 | GT first completions, some index timeouts |
| 01:33 | 32/300 | 16/300 | Both progressing steadily |
| 01:48 | 32/300 | 16/300 | — |
| 02:08 | 48/300 | 22/300 | — |
| 02:21 | 48/300 | 32/300 | — |
| 02:45 | 64/300 | 38/300 | Current |

---

## Bugs Fixed During Setup

1. **Indexer async timeout** — `_index_python_files` and `_resolve_python_imports` were synchronous, blocking the event loop. `asyncio.wait_for` couldn't cancel. Fixed by making them async with periodic `await asyncio.sleep(0)`.

2. **Risk scorer O(n^2)** — `handle_orient` called `risk_scorer.score_codebase()` which computed pairwise Levenshtein distance on all 29,834 symbols in Django. Fixed by skipping risk scoring for codebases with >5000 symbols.

3. **Missing `--dataset` CLI flag** — Runner hardcoded dataset. Added `--dataset` argument.

## Files Changed
- `benchmarks/swebench/runner.py` — `--dataset` flag
- `src/groundtruth/index/indexer.py` — async yields in Python indexing
- `src/groundtruth/mcp/tools.py` — skip risk scorer for large codebases
