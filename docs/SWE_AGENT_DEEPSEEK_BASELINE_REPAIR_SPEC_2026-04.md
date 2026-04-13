# SWE-agent + DeepSeek V3.2 Baseline Repair Spec

Date: April 13, 2026
Branch: `research/vnext-substrate-plan-2026-04-11`

## Objective

Define the canonical stage-1 baseline for:

- `SWE-agent`
- `DeepSeek V3.2`
- `SWE-bench-Live Lite`

This stage exists to answer one question before GT is reintroduced:

**Can we get plain SWE-agent + DeepSeek V3.2 into a believable neighborhood of the public Live Lite baseline?**

Only after that is true should GT be reintroduced as a hybrid delta.

## Verified public facts

- Public `SWE-agent + DeepSeek V3` on `SWE-bench-Live Lite` is `15.33% resolved`.
- The public SWE-agent docs recommend `function_calling` as the default parser and say `thought_action` should be used only if the model does not support function calling.
- DeepSeek V3.2 is available on Vertex AI MaaS.

Sources:

- `https://arxiv.org/abs/2505.23419`
- `https://swe-agent.com/latest/installation/keys/`
- `https://swe-agent.com/1.0/reference/parsers/`
- `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/maas/deepseek/deepseek-v32`

## Audit result

The branch currently does not have one clean, trustworthy canonical baseline path.

Instead it contains three conflicting lineages:

1. Custom in-repo pseudo-SWE-agent code:
   - `benchmarks/swebench/config.py`
   - `benchmarks/swebench/agent.py`
2. Old mini-swe-agent DeepSeek configs:
   - `benchmarks/swebench/swebench_deepseek_v3*.yaml`
3. Newer real SWE-agent runs happening outside those old local configs.

This means the branch can currently produce a run that looks like:

- `SWE-agent`
- `DeepSeek V3.2`
- `Live Lite`

while still inheriting the wrong assumptions from:

- mini-swe-agent
- old deterministic DeepSeek settings
- optional GT prompt/tool wording
- non-canonical parser choices

## Main baseline problems

### 1. The repo DeepSeek configs are mini-agent carryovers

These files are explicitly mini-swe-agent configs and should not be treated as canonical SWE-agent baseline definitions:

- `benchmarks/swebench/swebench_deepseek_v3.yaml`
- `benchmarks/swebench/swebench_deepseek_v3_gt_tools.yaml`
- `benchmarks/swebench/swebench_deepseek_v3_hybrid_v1.yaml`
- `benchmarks/swebench/swebench_deepseek_v3_hybrid_v2.yaml`

They also use `temperature: 0.0`, which is not the desired DeepSeek baseline operating point for this branch.

### 2. The custom in-repo benchmark agent is not the real target scaffold

These files are useful as experiments but should not define stage-1:

- `benchmarks/swebench/config.py`
- `benchmarks/swebench/agent.py`
- `benchmarks/swebench/runner.py`

They hardcode values like:

- `temperature: 0.0`
- `max_turns: 30`
- `max_cost_per_task: 0.50`

That is not the intended Live Lite baseline shape.

### 3. Parser choice is likely wrong

For stage-1 baseline repair, the parser should be:

- `function_calling`

not:

- `thought_action`

unless DeepSeek V3.2 in the actual serving path proves it cannot support function calling.

### 4. The branch lacks one canonical baseline launch path

There is no single repo-blessed path that says:

- this is the real SWE-agent launcher
- this is the real DeepSeek V3.2 config
- this is the real Live Lite baseline
- GT is completely off

Without that, every result remains confounded.

## Canonical stage-1 baseline definition

Use this as the branch standard until proven unstable by smoke data.

### Baseline shape

- scaffold: `SWE-agent`
- model: `DeepSeek V3.2`
- provider: `Vertex AI MaaS` via LiteLLM-compatible proxy
- dataset: `SWE-bench-Live/SWE-bench-Live`
- split: `lite`
- attempts per task: `1`
- workspace: `docker`
- parser: `function_calling`
- per-instance call limit: `100`

### Model parameters

- `temperature: 1.0`
- `top_p: 0.95`
- `max_output_tokens: 8192`

### Baseline purity rules

Forbidden in stage-1:

- GT tools
- GT prompt clauses
- GT state commands
- GT hooks
- GT ranking or veto logic

If GT is present at all, the run is not stage-1.

## Acceptance criteria for stage-1

Run a 10-task smoke first.

Track:

- patch rate
- zero-edit rate
- median turns before first edit
- empty/no-op patch rate
- infra failures
- token usage
- wall-clock runtime

### Smoke pass criteria

- parser is `function_calling`
- GT is fully disabled
- patch rate materially exceeds the broken run's `18-19%`
- run shape is stable enough to justify a 50-task canary

### Full baseline-readiness criteria

Do not claim the baseline is repaired unless:

- it is within a believable neighborhood of the public `15.33%`
- or its patch-production headroom makes that baseline plausible

If patch rate remains near `18-19%`, the baseline is still not repaired.

## Stage-2 handoff

Only after stage-1 is stable should GT come back in.

The intended stage-2 shape is:

- same scaffold
- same model
- same dataset / split
- same parser
- same call limit
- GT as the only intentional delta

That GT layer should be:

- one startup briefing
- sparse on-demand tools
- one final mandatory `gt_check`
- no spam

## Files to ignore when reasoning about stage-1

Ignore these as canonical baseline definitions:

- `benchmarks/swebench/swebench_deepseek_v3*.yaml`
- `benchmarks/swebench/run_live_baseline.py`
- `benchmarks/swebench/run_mini_gt_hybrid_v1.py`
- other mini-swe-agent launchers

They may still be useful as historical experiments only.
