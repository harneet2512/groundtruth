# GroundTruth Run Ledger

Real data from every evaluation run. Measured costs (DeepSeek balance delta), not estimates.

## Historical Runs (Session 2026-05-16/17)

| Run ID | Commit | Tasks | Result | Notes |
|---|---|---|---|---|
| 25967183060 | pre-session | 5 | 3/5 GT-on | Paired baseline |
| 25967190337 | pre-session | 5 | 3/5 Baseline | Same 3 resolve both arms |
| 25969723213 | 15f3840 | 5 | 3/5 | 0 flips, L3 regex contracts |
| 25971286734 | 1ff3df3 | 5 | 3/5 | 0 flips, L1 contracts |
| 25971993056 | 3d2c308 | 5 | 3/5 | 0 flips, doc filter |
| 25973765785 | 4d4a156 | 5 | 3/5 | 0 flips, literal caller code |
| 25975330305 | 5f52dca | 20 | 5/18 | L3 wiring bug |
| 25975336809 | 5f52dca | 20 | 7/20 | Baseline (20 tasks) |
| 25977165661 | 3951350 | 20 | 7/20 | Delivery confirmed working |
| 25978127934 | c8feb782 | 1 | 0/1 | REGRESSION: over-injection |
| 25978442722 | a79393c4 | 2 | 2/2 | Fixed with budget gates |

Estimated total cost (sessions 2026-05-16/17): ~$1.65

---

## Current Runs (Session 2026-05-26, commit c0817be7)

Fixes: stuck detector compat, L6 dead code removal, LIKE escaping, confidence filter, condenser OFF.

### Run 26427760181 — 2-task smoke

| Task | Resolved | F2P | P2P Fail | Calls | Input Tokens | Output Tokens |
|---|---|---|---|---|---|---|
| beets-5495 | **YES** | 4/4 | 0 | 68 | 1,290,251 | 9,141 |
| loguru-1297 | No | 0/4 | 0 | 32 | 603,816 | 4,730 |

### Run 26428772957 — 5-task smoke

| Task | Resolved | F2P | P2P Fail | Calls | Input Tokens | Output Tokens | Wall Time |
|---|---|---|---|---|---|---|---|
| beancount-931 | **YES** | 1/1 | 0 | 80 | 2,223,959 | 13,296 | 12m44s |
| beets-5495 | **YES** | 4/4 | 0 | 64 | 2,802,072 | 8,267 | 15m01s |
| loguru-1297 | No | 0/4 | 0 | 48 | 1,589,988 | 8,821 | 13m55s |
| loguru-1306 | No | 5/10 | 5 | 29 | 690,040 | 3,026 | 10m12s |
| WeasyPrint-2300 | INFRA FAIL | — | — | 0 | 0 | 0 | 1m38s |

### Run 26429285741 — WeasyPrint retry

| Task | Resolved | F2P | P2P Fail | Calls | Input Tokens | Output Tokens | Wall Time |
|---|---|---|---|---|---|---|---|
| WeasyPrint-2300 | pending | — | — | — | — | — | — |

## Measured Cost (DeepSeek API balance delta)

| Run | Balance Before | Balance After | Cost | Tasks | Per Task |
|---|---|---|---|---|---|
| 26427760181 | — | — | ~$0.03 est | 2 | ~$0.015 |
| 26428772957 | $27.01 | $26.95 | **$0.06** | 4 (1 infra fail) | **$0.015** |
| 26429285741 | $26.95 | pending | pending | 1 | pending |
| **Session total** | | | **$0.09+** | 7 | **$0.013** |

## Baseline Comparison

| Task | Baseline Resolved | Baseline Actions | GT Resolved | GT Actions (old) | Action Delta |
|---|---|---|---|---|---|
| beancount-931 | YES | 41 | YES | 29 | -12 (29% faster) |
| beets-5495 | YES | 51 | YES | 30 | -21 (41% faster) |
| cfn-lint-3821 | YES | 30 | YES | 26 | -4 (13% faster) |
| xarray-9760 | YES | 60 | YES | 60 | 0 |
| loguru-1306 | No | 31 | No | — | — |

**Flips: 0.** GT matches baseline resolution. Value is speed: 9.25 fewer actions on average.

## Config

- Model: deepseek-v4-flash (DeepSeek API direct)
- Condenser: OFF (DeepSeek prefix caching handles cost)
- Max iterations: 100
- Temperature: 0.7
- Agent: CodeActAgent (OpenHands)
- GT commit: c0817be7 (stuck detector fix + L6 removal + LIKE escaping + condenser off)

## Raw Data

Per-task JSONL: `RUN_LEDGER.jsonl`
| 2026-06-03T14:57Z | [26892084613](https://github.com/harneet2512/groundtruth/actions/runs/26892084613) | DeepSWE: Single GT trial | DeepSWE: Single GT trial | 4aa8167d | completed | success | YES | ? | `.claude/reports/runs/20260603_143841__DeepSWE__Single_GT_trial__26892084613__miniswe_v1_nocapture` |
| 2026-06-03T15:01Z | [26892081558](https://github.com/harneet2512/groundtruth/actions/runs/26892081558) | FINAL_ARCH_V2 Canary (2-3 task, 3-arm paired) | FINAL_ARCH_V2 Canary (2-3 task, 3-arm paired) | 4aa8167d | completed | success | ? | resolved=1 | `.claude/reports/runs/20260603_143838__FINAL_ARCH_V2_Canary_2-3_task_3-arm_paired__26892081558__OH_canary_v2live_beets5495` |
| 2026-06-03T15:07Z | [26893077974](https://github.com/harneet2512/groundtruth/actions/runs/26893077974) | DeepSWE: Single GT trial | DeepSWE: Single GT trial | b2ab925f | completed | success | YES | ? | `.claude/reports/runs/20260603_145501__DeepSWE__Single_GT_trial__26893077974__miniswe_v2_capture` |
