# DeepSWE Baseline — Model & Agent Configuration

**Date:** 2026-05-29T07:18Z
**Run:** `deepswe_baseline.yml` (GHA, `workflow_dispatch`)
**Goal:** Establish the no-GT resolution rate on DeepSWE with **deepseek-v4-flash, thinking disabled**, so it can later be compared against the GT-augmented arm.

---

## 1. What datacurve used (the reference)

| Factor | datacurve leaderboard | Source |
|---|---|---|
| Harness | **mini-swe-agent** (via Pier on Modal) | deepswe.datacurve.ai ("All models are run with mini-swe-agent") |
| Verifier | per-task `tests/test.sh` → `reward.txt` 1/0 (base AND new tests pass) | `deep-swe/README.md`, task `tests/test.sh` |
| Headline model | **deepseek-v4-pro** | deepswe.datacurve.ai |
| Their pro score | **8% ± 2%** | deepswe.datacurve.ai |
| Thinking | **enabled** (V4 default; temperature ignored in this mode) | api-docs.deepseek.com/guides/thinking_mode |
| Temperature | N/A — ignored under thinking mode | DeepSeek thinking-mode docs |
| Attempts | pass@1 | leaderboard convention |

Datacurve did **not** publish temperature/step-limit on the landing page; their pro runs were thinking-ON, so temperature was a no-op for them.

## 2. What WE run (and why it deviates)

| Factor | Our baseline | Rationale |
|---|---|---|
| Harness | mini-swe-agent (stock, no GT) | Same harness as datacurve + as the GT arm → clean A/B |
| Model | **deepseek/deepseek-v4-flash** | User directive: flash, not pro (cheaper; measures the model we'll pair with GT) |
| **Thinking** | **DISABLED** (`extra_body.thinking.type = disabled`) | User directive. Default is enabled; disabled via the raw documented API param |
| **Temperature** | **0.0** | This IS the official mini-swe-agent default (`mini.yaml` + `swebench.yaml` both set `temperature: 0.0`); datacurve set no override. Thinking-off makes it active; DeepSeek also recommends 0.0 for coding. |
| `drop_params` | **true** | Official mini-swe-agent default; lets litellm drop provider-unsupported params (does not affect `extra_body` thinking-disable). |
| top_p | default (unset) | With temp=0 (greedy), top_p is moot; leaving default avoids over-constraining |
| **Turns (`step_limit`)** | **300** | Matches the GT arm's budget (`deepswe_gt_pier.yaml`) so turn budget isn't a confound |
| `cost_limit` | **$5.0 / task** | Matches GT arm; flash is cheap so this is a safety ceiling, not a binder |
| Per-command timeout | 120s | mini-swe-agent / GT-arm default |
| Agent timeout | 5400s (90 min) | from each task's `task.toml [agent]` |
| Verifier timeout | 1800s (30 min) | from each task's `task.toml [verifier]` |
| Image pull timeout | 900s | GT-arm default |
| Attempts | **pass@1** (`-k 1`) | Single deterministic attempt |
| CPU / RAM (per container) | 2 cpu / 8192 MB | `task.toml`; pier passes `--cpus`/`--memory` to docker |
| Storage | 20480 MB (advisory) | `task.toml`; **not** enforced by pier docker → only host disk matters |
| `allow_internet` | false (per task) | Pier opens a per-agent allowlist only for the LLM call |

### ⚠️ Comparability caveat

Our **flash + thinking-OFF** number is **NOT** directly comparable to datacurve's **pro + thinking-ON 8%**. Two variables differ (model tier AND thinking). This baseline is the correct comparison point for **our GT arm** (which must use the *same* flash + thinking-off config), not for the public leaderboard.

## 3. How thinking is disabled (exact mechanism)

- DeepSeek V4 default: thinking **enabled**. Disable with `{"thinking": {"type": "disabled"}}` (api-docs.deepseek.com/guides/thinking_mode).
- litellm's `reasoning_effort="none"` is *supposed* to map to this but is buggy (BerriAI/litellm #27453, #27439) — so we pass the param **directly** via `model_kwargs.extra_body`, which litellm forwards verbatim to the DeepSeek (OpenAI-compatible) endpoint.
- Config lands via `--ak config_file=.github/configs/deepswe_baseline_mini.yaml`, which Pier writes into the container as `custom.yaml` and layers over `mini.yaml`.

## 4. Preflight gate

Before any task container spins up, the workflow's `preflight` job does one `litellm.completion` against **flash with `extra_body.thinking.type=disabled` and temperature 0.0**. If the model id or thinking-disable param is rejected, the whole run fails for ~$0 instead of burning 5+ task containers.

## Sources

- [DeepSWE leaderboard](https://deepswe.datacurve.ai/) — mini-swe-agent harness; deepseek-v4-pro 8%±2%
- [deep-swe repo](https://github.com/datacurve-ai/deep-swe) — task format, verifier
- [DeepSeek thinking-mode docs](https://api-docs.deepseek.com/guides/thinking_mode) — `thinking:{type:disabled}`, temperature ignored under thinking
- [litellm #27453](https://github.com/BerriAI/litellm/issues/27453), [#27439](https://github.com/BerriAI/litellm/issues/27439) — reasoning_effort→thinking mapping bugs
- `artifact_deepswe/gt_integration/deepswe_gt_pier.yaml` — GT-arm budget (step_limit 300, cost_limit 5.0, temp 1.0[no-op])
- `pier/agents/installed/mini_swe_agent.py` — config mechanism (`model_kwargs`, `config_file`, `reasoning_effort`)
