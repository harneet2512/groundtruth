# METRICS_EXPLAINED.md — what we log per run, and how cost is computed

Every benchmark run produces a metrics record so anyone can open the repo and see
exactly what it cost, how many tokens it used, how the agent behaved, and which GT
layers fired — reproducibly, from the run artifacts alone.

## How to generate it

```bash
gh run download <run-id> -D /tmp/run_artifacts          # the task-<id>/ artifacts
python scripts/metrics/compute_run_metrics.py \
    --artifacts /tmp/run_artifacts \
    --model deepseek-v4-flash \
    --run-id <run-id> \
    --out-json RUN_METRICS.json --out-md RUN_METRICS.md
```

Re-running on the same artifacts yields identical numbers (deterministic).

## Where the numbers come from

| field | source |
|---|---|
| `prompt_tokens`, `completion_tokens`, `cache_read_tokens` | the LAST cumulative `llm_metrics.accumulated_token_usage` in the agent trajectory (`output.jsonl`) |
| `action_count`, `edit_count`, `has_patch` | the agent `history[]` (actions excluding think/recall/message) |
| `resolved` | the official grader output `eval_result.json` (`RESOLVED` / `NO` / `no_report`) |
| `gt_layers_emitted` | `gt_layer_events_*.jsonl` (cross-reference only — "emitted", not "delivered") |
| `cost_usd` | **computed** from token counts × pricing (see below) |

## Why cost is COMPUTED, not read from the API

litellm and OpenRouter both return **null cost for `deepseek-v4-flash`** (the rate isn't
in their cost map), so OpenHands' `metrics.accumulated_cost` is `0.0`. The **token counts
are captured**, so we compute cost deterministically:

```
billable_input = prompt_tokens - cache_read_tokens     # cache-MISS prompt tokens
cost = billable_input  / 1e6 * input_per_1m
     + cache_read_tokens / 1e6 * cache_hit_per_1m       # cached prompt tokens are cheaper
     + completion_tokens / 1e6 * output_per_1m
```

`prompt_tokens` is the TOTAL prompt; `cache_read_tokens` is the portion served from the
prompt cache (billed at the cheaper cache-hit rate). Output (completion) tokens are billed
at the output rate.

## Pricing

Rates live in `benchmarks/pricing/deepseek_pricing.json` (USD per 1M tokens), so the cost
is auditable and you can drop in your exact contract rate:

```
deepseek-v4-flash:  input 0.27 | cache-hit 0.07 | output 1.10
```

These default to DeepSeek's published `deepseek-chat` tiers (the v4-flash rate is not
published as of this writing). **Override with your real API/contract rate for an exact
figure.** Because the formula is explicit and the tokens are captured, swapping rates
re-prices any past run without re-running it.

## What "resolved" means (and doesn't)

`resolved` is the official SWE-bench-Live grader verdict (did the agent's patch flip the
hidden FAIL_TO_PASS tests without breaking PASS_TO_PASS). It is the OUTCOME. It is **not**
the same as "GT worked" — GT correctness is verified separately from the agent's
trajectory against `gt_gt.md` (see the ramp verification strategy). A resolved task is
only a GT *flip* if the GT-OFF baseline could NOT resolve it.

## Example (Phase-2 ramp run)

```
model: deepseek-v4-flash | 3 tasks | resolved 1/3
total cost: $0.345258 (mean $0.115/task)
tokens: prompt 4.25M | completion 28.6k | cache-read 4.17M (96% cache-hit)
```

The 96% cache-read rate is the prompt cache working — most of each prompt is billed at the
cheap cache-hit rate, so cost is dominated by the cache-miss input + the output tokens.
