# PART9: GT v4.2 Full 300-Task Results

## Date: 2026-03-18

---

## Core Numbers

| Metric | Baseline | GT v4.2 | Delta |
|--------|----------|---------|-------|
| **Resolved** | **113/300** | **105/300** | **-8** |
| Resolve rate | 37.7% | 35.0% | -2.7pp |
| Patches produced | 293 | 297 | +4 |
| Empty patches | 7 | 3 | -4 |
| Eval errors | 3 | 5 | +2 |
| Runtime | 1h 15m | 1h 25m | +10m |

**Net result: GT v4.2 resolves 8 fewer tasks than baseline.**

---

## Per-Task Breakdown

| Category | Count | % |
|----------|-------|---|
| Both resolved | 85 | 28.3% |
| GT gained (not in baseline) | 20 | 6.7% |
| GT lost (in baseline, not GT) | 28 | 9.3% |
| Neither resolved | 167 | 55.7% |

### Tasks GT Gained (+20)

| Instance ID | GT Tool Calls | Commands Used |
|-------------|--------------|---------------|
| django__django-10924 | 24 | references(15), outline(9) |
| django__django-12284 | 24 | references(11), impact(7), outline(6) |
| django__django-12497 | 3 | outline(3) |
| django__django-12708 | 10 | references(4), impact(3), outline(3) |
| django__django-12856 | 0 | (no GT used) |
| django__django-12908 | 7 | outline(3), impact(3), references(1) |
| django__django-12983 | 15 | outline(9), references(6) |
| django__django-13757 | 15 | references(9), outline(6) |
| django__django-15789 | 6 | outline(3), references(3) |
| django__django-15790 | 6 | outline(3), references(3) |
| pydata__xarray-5131 | 3 | outline(3) |
| pylint-dev__pylint-5859 | 0 | (no GT used) |
| scikit-learn__scikit-learn-13142 | 15 | outline(9), impact(3), references(3) |
| scikit-learn__scikit-learn-14092 | 0 | (no GT used) |
| sphinx-doc__sphinx-8435 | 0 | (no GT used) |
| sympy__sympy-13773 | 15 | outline(9), references(6) |
| sympy__sympy-15345 | 0 | (no GT used) |
| sympy__sympy-15346 | 13 | references(10), outline(3) |
| sympy__sympy-21379 | 0 | (no GT used) |
| sympy__sympy-21847 | 0 | (no GT used) |

**13/20 gained tasks actively used GT tools.** The 7 that gained without GT may be due to the different system prompt or temperature change (non-determinism).

### Tasks GT Lost (-28)

| Instance ID | GT Tool Calls | Commands Used |
|-------------|--------------|---------------|
| astropy__astropy-6938 | 3 | outline(3) |
| django__django-11583 | 3 | outline(3) |
| django__django-11848 | 0 | (no GT used) |
| django__django-12125 | 30 | outline(21), references(9) |
| django__django-13028 | 7 | impact(3), references(4) |
| django__django-13401 | 9 | references(6), impact(3) |
| django__django-13551 | 24 | impact(9), references(9), outline(6) |
| django__django-13710 | 22 | references(10), outline(9), impact(3) |
| django__django-14787 | 0 | (no GT used) |
| django__django-14915 | 3 | impact(3) |
| django__django-15814 | 3 | outline(3) |
| matplotlib__matplotlib-25311 | 27 | outline(24), references(3) |
| matplotlib__matplotlib-26011 | 6 | outline(6) |
| pytest-dev__pytest-5495 | 6 | outline(3), references(3) |
| pytest-dev__pytest-7490 | 0 | (no GT used) |
| scikit-learn__scikit-learn-13584 | 9 | outline(3), references(6) |
| scikit-learn__scikit-learn-14894 | 3 | outline(3) |
| scikit-learn__scikit-learn-15535 | 0 | (no GT used) |
| sphinx-doc__sphinx-11445 | 9 | outline(6), references(3) |
| sympy__sympy-15011 | 0 | (no GT used) |
| sympy__sympy-15609 | 3 | outline(3) |
| sympy__sympy-16792 | 10 | outline(3), impact(3), references(4) |
| sympy__sympy-17139 | 9 | outline(6), references(3) |
| sympy__sympy-18189 | 12 | outline(9), references(3) |
| sympy__sympy-18532 | 16 | outline(9), impact(3), references(4) |
| sympy__sympy-20212 | 15 | outline(3), impact(3), references(9) |
| sympy__sympy-20590 | 13 | outline(7), impact(5), references(1) |
| sympy__sympy-22714 | 6 | outline(3), references(3) |

**22/28 lost tasks actively used GT tools.** This is the key concern — heavy GT usage correlated with losses, not gains. Several lost tasks had very high call counts (django-12125: 30 calls, matplotlib-25311: 27 calls, django-13551: 24 calls).

---

## Tool Usage Metrics (GT Condition Only)

### Adoption
- **59.0% of tasks** used gt_tool.py (177/300)
- Total GT tool calls: **1618**
- Avg calls per task (all): 5.4
- Avg calls per task (users only): 9.1

### Command Distribution

| Command | Calls | % | Purpose |
|---------|-------|---|---------|
| outline | 844 | 52.2% | File structure overview |
| references | 563 | 34.8% | Symbol usage search |
| impact | 205 | 12.7% | Change scope analysis |
| check | 4 | 0.2% | Edit verification |
| diagnose | 2 | 0.1% | Syntax/error check |

The prompt change worked: check dropped from 249 (v4.1) to 4 (v4.2), references went from 11 to 563. Agent is now exploring, not validating. But the exploration is hurting more than helping.

### Resolve Rate by GT Usage

| Category | Resolved | Total | Rate |
|----------|----------|-------|------|
| Used GT tool | 74 | 177 | **41.8%** |
| Did NOT use GT | 31 | 123 | **25.2%** |

**Tasks where the agent used GT resolved at 41.8% vs 25.2% for non-GT tasks.** This 16.6pp gap suggests GT tools ARE providing value — but this is confounded by task difficulty (easier tasks may naturally attract more tool use because the agent has more budget to explore).

---

## Comparison With All Previous Runs

| Version | GT | Baseline | Delta | Notes |
|---------|-----|----------|-------|-------|
| v4.0 (10 tasks) | 7/10 | 6/10 | **+1** | Small sample, promising |
| v4.1 (300 tasks) | 73/300 | 76/300 | **-3** | Validation-heavy (check: 249 calls) |
| v4.2 (300 tasks) | 105/300 | 113/300 | **-8** | Exploration-focused, clean infra |

### Infrastructure Health

| Metric | v4.1 | v4.2 Baseline | v4.2 GT |
|--------|------|---------------|---------|
| Total tasks | 300 | 300 | 300 |
| Patches produced | ~156 | 293 | 297 |
| Empty (Docker fails) | 144 | 7 | 3 |
| Docker errors | Many | 0 | 0 |

**Infrastructure is fully fixed.** The v4.1 run's 144 empty patches inflated both conditions equally, making comparison unreliable. These v4.2 results are the first clean comparison.

---

## Key Findings

### 1. GT Tool Usage Is Consuming Steps
The 10-minute longer runtime and the high call counts on lost tasks suggest GT is eating step budget. Tasks like django-12125 (30 GT calls) and matplotlib-25311 (27 calls) may have spent too many turns exploring and not enough fixing.

### 2. Outline Is Overused
52% of calls are `outline` — a command that duplicates what `cat` and `head` already do. The agent may be using GT outline as a slower substitute for standard file reading, wasting turns.

### 3. The System Prompt Adds Cognitive Load
Even though we removed validation directives, the GT tool section adds ~100 tokens to the prompt. For a small model like gpt-5.4-nano, every token of prompt space matters. The tool instructions may be displacing useful context.

### 4. Model Stochasticity
7 gained tasks and 6 lost tasks had zero GT calls. These deltas are purely from non-determinism (temperature was removed but the model is still stochastic). This means the "true" GT signal is closer to: +13 gained, -22 lost = -9.

### 5. GT Provides Genuine Value for Some Tasks
The 41.8% vs 25.2% resolve rate gap for GT-using vs non-GT tasks shows the tools help in many cases. The problem is the net negative from step budget waste and potential over-exploration on hard tasks.

---

## Hypotheses for Why GT Hurts Net Performance

1. **Step budget waste**: GT calls take turns. On tasks the agent would have solved anyway, GT exploration is pure overhead. On hard tasks, it burns budget without convergence.

2. **Outline redundancy**: `outline` is the most called command but provides the least unique value — the agent can already `cat` files. It may be a habitual call rather than a strategic one.

3. **Prompt size**: The tool instructions occupy prompt space that could hold problem context. For a nano model, this may cross a threshold.

4. **Over-exploration trap**: When the agent sees structural tools, it may over-index on understanding the codebase rather than quickly testing a fix. Some SWE-bench tasks are better solved by trial-and-error than by careful analysis.

---

## What This Means

GT v4.2 with exploration-only tools **does not beat baseline** on SWE-bench Lite with gpt-5.4-nano. The tool provides genuine help on ~13 tasks but causes harm on ~22, for a net -9 (excluding stochastic variation).

### Not Ready for Leaderboard Submission

The results do not support submission. GT needs to demonstrate a positive delta before we submit.

### Next Steps (if pursuing further)

1. **Remove outline from prompt** — it duplicates standard file reading and accounts for 52% of wasted calls
2. **Make tools opt-in** — mention tools exist but don't list syntax in prompt. Let agent discover via `python3 /tmp/gt_tool.py --help`
3. **Test with a larger model** — gpt-5.4-nano may not have enough capacity to productively use structural tools. Try gpt-5.4-mini or Claude Sonnet
4. **Selective tool injection** — only inject tools for tasks with high file count or complex class hierarchies where exploration genuinely helps
5. **Measure step waste** — compare avg turns to resolution between GT and baseline to quantify the budget cost

---

## Raw Data Locations (VM: 34.122.24.67)

- Baseline predictions: `~/baseline_v42/preds.json`
- Baseline trajectories: `~/baseline_v42/*//*.traj.json`
- Baseline eval: `~/baseline_v42.baseline_v42.json`
- GT predictions: `~/gt_v42/preds.json`
- GT trajectories: `~/gt_v42/*//*.traj.json`
- GT eval: `~/gt_v42.gt_v42.json`
