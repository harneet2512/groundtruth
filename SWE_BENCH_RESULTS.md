# GroundTruth SWE-bench Verified Experiment Results

## Summary

| Condition | Resolved | Total | Rate |
|-----------|----------|-------|------|
| **Baseline** | 288 | 500 | **57.6%** |
| **GT (live tools)** | 285 | 500 | **57.0%** |
| **Delta** | -3 | | **-0.6%** |

**GroundTruth live tools produced no measurable lift on SWE-bench Verified.**

The model called GT tools 749 times across 500 tasks (~1.5 calls/task) with zero errors, but the additional codebase intelligence did not translate to more resolved tasks.

---

## Experiment Design

### What We Tested

Whether giving an AI coding agent access to GroundTruth's codebase intelligence tools (obligation analysis, reference lookup, patch completeness checking) improves its ability to resolve real GitHub issues.

### Conditions

**Baseline:** Standard SWE-bench agent with bash, python, text_editor tools.

**GT (treatment):** Identical agent with three additional tools:
- `gt_impact(symbol)` — Shows obligation sites, shared state, subclass overrides
- `gt_references(symbol)` — Shows where a symbol is defined and all usage sites
- `gt_check()` — Verifies patch covers all obligation sites

The GT tools were native Inspect AI tool calls, not prompt injection. The model decided when and whether to call them.

### Configuration (identical for both conditions)

| Parameter | Value |
|-----------|-------|
| Model | Qwen3-Coder-480B via Vertex AI |
| Scaffold | Inspect AI (inspect-evals) |
| Docker images | Epoch AI pre-built (ghcr.io/epoch-research/) |
| Dataset | SWE-bench Verified (500 tasks) |
| Temperature | 0.7 |
| Top-p | 0.8 |
| Message limit | 100 |
| Max connections | 4 per VM (8 total) |
| VMs | 2x e2-standard-8 (GCP) |
| Sharding | Deterministic alphabetical split, 250/shard |

### What Differed

The ONLY difference between conditions was the tool list. GT added `gt_impact`, `gt_references`, and `gt_check` alongside the standard tools, plus a short system prompt paragraph explaining their purpose.

---

## GT Tool Usage Analysis

### Call Volume

| Metric | Shard A | Shard B | Total |
|--------|---------|---------|-------|
| Total calls | 488 | 261 | **749** |
| Calls per task | 1.95 | 1.04 | **1.50** |
| Unique symbols | 196 | 120 | ~300 |
| Error rate | 0/488 | 0/261 | **0%** |

### Tool Breakdown

| Tool | Shard A | Shard B | Total | Share |
|------|---------|---------|-------|-------|
| gt_check | 199 | 104 | 303 | 40% |
| gt_references | 167 | 92 | 259 | 35% |
| gt_impact | 122 | 65 | 187 | 25% |

### Performance

| Metric | Shard A | Shard B |
|--------|---------|---------|
| Avg duration | 7.8s | 5.9s |
| Max duration | 34.5s | 67.9s |

### Per-Shard Results

| | Baseline Resolved | GT Resolved | Delta |
|---|---|---|---|
| Shard A (django + astropy) | 147/250 (59%) | 146/250 (58%) | -1 |
| Shard B (sympy + matplotlib + sklearn + ...) | 141/250 (56%) | 139/250 (56%) | -2 |

---

## Why GT Didn't Help

### 1. The model already solves the tasks it can solve

At 57-58% resolve rate, the model succeeds on tasks where it can understand the issue, locate the code, and write a correct fix within 100 messages. GT's codebase intelligence doesn't help with the fundamental capability gap on the remaining 42%.

### 2. GT tools are slow relative to the model's workflow

Average GT call takes 6-8 seconds. The model's bash/grep workflow returns in <1 second. When the model can find code with `grep -rn "ClassName"` in 200ms, spending 8 seconds on `gt_references("ClassName")` is a net loss of time budget within the 100-message limit.

### 3. Obligation analysis addresses a rare failure mode

GT's core value proposition — finding coupled sites that must change together — only applies to a subset of SWE-bench tasks. Most issues require understanding the bug and writing a targeted fix, not finding all obligation sites. The tasks where obligation analysis would help (multi-site coordinated changes) are a small fraction of the 500.

### 4. The model doesn't trust GT output over its own exploration

In observed traces, the model calls GT tools but then continues its own grep/read workflow regardless. GT's output supplements but doesn't redirect the model's behavior in a way that changes outcomes.

### 5. Tool call overhead consumes message budget

Each GT tool call consumes 1-2 messages (call + response). With 749 calls across 500 tasks, GT consumed ~1500 messages that could have been used for additional exploration, testing, or fix attempts. This may explain the slight negative delta.

---

## Comparison to Published Scores

| Model/Scaffold | SWE-bench Verified |
|---|---|
| Qwen3-Coder + OpenHands (official) | **67.0%** |
| Qwen3-Coder + OpenHands 500-turn | **69.6%** |
| **Qwen3-Coder + Inspect (this run, baseline)** | **57.6%** |
| **Qwen3-Coder + Inspect + GT (this run)** | **57.0%** |

Our baseline underperforms the published 67% by ~10 points. This is due to:
- Inspect scaffold vs OpenHands (generic tool calling vs Qwen3-Coder native tool formatter)
- Missing `top_k=20` and `repetition_penalty=1.05` (not supported by Inspect CLI)
- 100 message limit vs 500 turns in the extended OpenHands config
- Routing through litellm proxy vs direct Vertex access

The GT comparison is valid (same scaffold, same params) even though the absolute scores are lower than leaderboard.

---

## Infrastructure

### Timeline

| Phase | Duration |
|-------|----------|
| VM setup + Inspect install | 30 min |
| Image pulls (cached after first) | 45 min |
| Baseline inference (500 tasks) | ~2.5 hrs |
| GT inference (500 tasks) | ~2.5 hrs |
| **Total** | **~6 hrs** |

### Cost

| Item | Cost |
|------|------|
| 2x e2-standard-8 VMs (~8 hrs) | ~$4.30 |
| Vertex AI API (1000 task runs) | ~$60 |
| Disk (500GB x 2) | ~$2 |
| **Total** | **~$66** |

---

## What This Means for GroundTruth

### The null result is informative

GT tools work correctly (0% error rate), the model uses them voluntarily (749 calls), and the information returned is structurally sound. The problem is not implementation — it's product-market fit for this specific use case.

### SWE-bench is not GT's target scenario

SWE-bench tasks are single-issue fixes where the model works alone. GT's value proposition — preventing hallucinated imports, catching missed obligation sites, providing codebase-wide context — matters more in:
- Multi-file refactors
- Large codebase navigation (>100K LOC)
- Team collaboration where context is fragmented
- Ongoing development (not one-shot fixes)

### Next steps

1. **Don't optimize GT for SWE-bench.** The benchmark doesn't exercise GT's strengths.
2. **Test on real-world multi-file tasks** where obligation analysis actually prevents incomplete changes.
3. **Measure GT on agent workflows** where context window limits cause hallucinations (the original motivation).
4. **Consider MCP-native integration** where GT is always available, not just for benchmarks.

---

## Raw Data

- Baseline eval logs: `baseline-shard-a:/home/Lenovo/results/baseline/`, `baseline-shard-b:/home/Lenovo/results/baseline/`
- GT eval logs: `baseline-shard-a:/home/Lenovo/results/gt/`, `baseline-shard-b:/home/Lenovo/results/gt/`
- GT tool call logs: `baseline-shard-a:/tmp/gt_tool_calls_full.jsonl`, `baseline-shard-b:/tmp/gt_tool_calls_full.jsonl`
- VMs: `baseline-shard-a`, `baseline-shard-b` (GCP us-central1-a, project serious-water-484116-j0)
- Date: 2026-03-22
